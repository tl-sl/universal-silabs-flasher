from __future__ import annotations

import asyncio
import dataclasses
import logging
import typing

import async_timeout
import bellows.config
import bellows.ezsp
import bellows.types

from .common import (
    PROBE_TIMEOUT,
    SerialProtocol,
    Version,
    connect_protocol,
    pad_to_multiple,
)
from .const import DEFAULT_BAUDRATES, GPIO_CONFIGS, ApplicationType, ResetTarget
from .cpc import CPCProtocol
from .emberznet import connect_ezsp
from .firmware import FirmwareImage
from .gecko_bootloader import GeckoBootloaderProtocol, NoFirmwareError
from .gpio import send_gpio_pattern
from .spinel import SpinelProtocol
from .xmodemcrc import BLOCK_SIZE as XMODEM_BLOCK_SIZE

_LOGGER = logging.getLogger(__name__)

EZSP_BOOTLOADER_LAUNCH_DELAY = 5


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    version: Version | None
    continue_probing: bool
    baudrate: int


class Flasher:
    def __init__(
        self,
        *,
        baudrates: dict[ApplicationType, list[int]] = DEFAULT_BAUDRATES,
        probe_methods: tuple[ApplicationType, ...] = (
            ApplicationType.GECKO_BOOTLOADER,
            ApplicationType.CPC,
            ApplicationType.EZSP,
            ApplicationType.SPINEL,
        ),
        device: str,
        bootloader_reset: str | None = None,
    ):
        self._baudrates = baudrates
        self._probe_methods = probe_methods
        self._device = device

        self.app_type: ApplicationType | None = None
        self.app_version: Version | None = None
        self.app_baudrate: int | None = None
        self.bootloader_baudrate: int | None = None

        self._reset_target: ResetTarget | None = (
            ResetTarget(bootloader_reset) if bootloader_reset else None
        )

    async def enter_bootloader_reset(self, target):
        _LOGGER.info(f"Triggering {target.value} bootloader")
        if target in GPIO_CONFIGS.keys():
            config = GPIO_CONFIGS[target]
            await send_gpio_pattern(
                config["chip"], config["pin_states"], config["toggle_delay"]
            )
        else:
            await self.enter_serial_bootloader()

    async def enter_serial_bootloader(self):
        baudrate = self._baudrates[ApplicationType.GECKO_BOOTLOADER][0]
        async with connect_protocol(self._device, baudrate, SerialProtocol) as sonoff:
            serial = sonoff._transport.serial
            serial.dtr = False
            serial.rts = True
            await asyncio.sleep(0.1)
            serial.dtr = True
            serial.rts = False
            await asyncio.sleep(0.5)
            serial.dtr = False

    def _connect_gecko_bootloader(self, baudrate: int):
        return connect_protocol(self._device, baudrate, GeckoBootloaderProtocol)

    def _connect_cpc(self, baudrate: int):
        return connect_protocol(self._device, baudrate, CPCProtocol)

    def _connect_ezsp(self, baudrate: int):
        return connect_ezsp(self._device, baudrate)

    def _connect_spinel(self, baudrate: int):
        return connect_protocol(self._device, baudrate, SpinelProtocol)

    async def probe_gecko_bootloader(
        self, *, baudrate: int, run_firmware: bool = True
    ) -> ProbeResult:
        try:
            async with self._connect_gecko_bootloader(baudrate) as gecko:
                bootloader_version = await gecko.probe()

                if run_firmware:
                    await gecko.run_firmware()
                    _LOGGER.info("Launched application from bootloader")

            await asyncio.sleep(1)
        except NoFirmwareError:
            _LOGGER.warning("No application can be launched")
            return ProbeResult(
                version=bootloader_version,
                baudrate=baudrate,
                continue_probing=False,
            )
        else:
            return ProbeResult(
                version=bootloader_version,
                baudrate=baudrate,
                continue_probing=run_firmware,
            )

    async def probe_cpc(self, baudrate: int) -> ProbeResult:
        async with self._connect_cpc(baudrate) as cpc:
            version = await cpc.probe()

        return ProbeResult(
            version=version,
            baudrate=baudrate,
            continue_probing=False,
        )

    async def probe_ezsp(self, baudrate: int) -> ProbeResult:
        async with self._connect_ezsp(baudrate) as ezsp:
            _, _, version = await ezsp.get_board_info()

        return ProbeResult(
            version=Version(version),
            baudrate=baudrate,
            continue_probing=False,
        )

    async def probe_spinel(self, baudrate: int) -> ProbeResult:
        async with self._connect_spinel(baudrate) as spinel:
            version = await spinel.probe()

        return ProbeResult(
            version=version,
            baudrate=baudrate,
            continue_probing=False,
        )

    async def probe_app_type(
        self,
        types: typing.Iterable[ApplicationType] | None = None,
    ) -> None:
        if types is None:
            types = self._probe_methods

        # Reset into bootloader
        if self._reset_target:
            await self.enter_bootloader_reset(self._reset_target)

        bootloader_probe = None

        # Only run firmware from the bootloader if we have bootloader reset and
        # other probe methods
        only_probe_bootloader = types == [ApplicationType.GECKO_BOOTLOADER]
        run_firmware = self._reset_target and not only_probe_bootloader
        probe_funcs = {
            ApplicationType.GECKO_BOOTLOADER: (
                lambda baudrate: self.probe_gecko_bootloader(
                    run_firmware=run_firmware, baudrate=baudrate
                )
            ),
            ApplicationType.CPC: self.probe_cpc,
            ApplicationType.EZSP: self.probe_ezsp,
            ApplicationType.SPINEL: self.probe_spinel,
        }

        for probe_method, baudrate in (
            (m, b) for m in types for b in self._baudrates[m]
        ):
            # Don't probe the bootloader twice
            if (
                probe_method == ApplicationType.GECKO_BOOTLOADER
                and bootloader_probe is not None
            ):
                _LOGGER.debug("Not probing bootloader twice")
                continue

            _LOGGER.info("Probing %s at %d baud", probe_method, baudrate)

            try:
                result = await probe_funcs[probe_method](baudrate=baudrate)
            except asyncio.TimeoutError:
                continue

            # Keep track of the bootloader version for later
            if probe_method == ApplicationType.GECKO_BOOTLOADER:
                _LOGGER.info("Detected bootloader version %s", result.version)
                bootloader_probe = result
                self.bootloader_baudrate = bootloader_probe.baudrate

            if result.continue_probing:
                continue

            self.app_type = probe_method
            self.app_version = result.version
            self.app_baudrate = result.baudrate
            break
        else:
            if bootloader_probe and self._reset_target:
                # We have no valid application image but can still re-enter the
                # bootloader
                await self.enter_bootloader_reset(self._reset_target)

                self.app_type = ApplicationType.GECKO_BOOTLOADER
                self.app_version = bootloader_probe.version
                self.app_baudrate = bootloader_probe.baudrate
                self.bootloader_baudrate = bootloader_probe.baudrate
                _LOGGER.warning("Bootloader did not launch a valid application")
            else:
                raise RuntimeError("Failed to probe running application type")

        _LOGGER.info(
            "Detected %s, version %s at %s baudrate (bootloader baudrate %s)",
            self.app_type,
            self.app_version,
            self.app_baudrate,
            self.bootloader_baudrate,
        )

    async def enter_bootloader(self) -> None:
        if self.app_type is None:
            await self.probe_app_type()

        if self.app_type is ApplicationType.GECKO_BOOTLOADER:
            # No firmware
            pass
        elif self.app_type is ApplicationType.CPC:
            async with self._connect_cpc(self.app_baudrate) as cpc:
                async with async_timeout.timeout(PROBE_TIMEOUT):
                    await cpc.enter_bootloader()
        elif self.app_type is ApplicationType.SPINEL:
            async with self._connect_spinel(self.app_baudrate) as spinel:
                async with async_timeout.timeout(PROBE_TIMEOUT):
                    await spinel.enter_bootloader()
        elif self.app_type is ApplicationType.EZSP:
            async with self._connect_ezsp(self.app_baudrate) as ezsp:
                try:
                    res = await ezsp.launchStandaloneBootloader(0x01)
                except asyncio.TimeoutError:
                    _LOGGER.warning(
                        "Application failed to respond to bootloader launching command."
                        " Assuming bootloader has launched."
                    )
                else:
                    if res[0] != bellows.types.EmberStatus.SUCCESS:
                        raise RuntimeError(
                            f"EmberZNet could not enter the bootloader: {res[0]!r}"
                        )

                    await asyncio.sleep(EZSP_BOOTLOADER_LAUNCH_DELAY)
        else:
            raise RuntimeError(f"Invalid application type: {self.app_type}")

        # Probe the bootloader baudrate
        if self.bootloader_baudrate is None:
            await self.probe_app_type(types=[ApplicationType.GECKO_BOOTLOADER])

    async def flash_firmware(
        self,
        firmware: FirmwareImage,
        run_firmware: bool = True,
        progress_callback: typing.Callable[[int, int], typing.Any] | None = None,
    ) -> None:
        data = firmware.serialize()

        # Pad the image to the XMODEM block size
        data = pad_to_multiple(data, XMODEM_BLOCK_SIZE, b"\xFF")

        async with self._connect_gecko_bootloader(self.bootloader_baudrate) as gecko:
            await gecko.probe()
            await gecko.upload_firmware(data, progress_callback=progress_callback)

            if run_firmware:
                await gecko.run_firmware()

    async def dump_emberznet_config(self) -> None:
        if self.app_type != ApplicationType.EZSP:
            raise RuntimeError(f"Device is not running EmberZNet: {self.app_type}")

        async with self._connect_ezsp(self.app_baudrate) as ezsp:
            for config in ezsp.types.EzspConfigId:
                v = await ezsp.getConfigurationValue(config)
                if v[0] == bellows.types.EzspStatus.ERROR_INVALID_ID:
                    continue
                print(f"{config.name}={v[1]}")

    async def write_emberznet_eui64(self, new_eui64: bellows.types.EUI64) -> bool:
        await self.probe_app_type()

        if self.app_type != ApplicationType.EZSP:
            raise RuntimeError(f"Device is not running EmberZNet: {self.app_type}")

        async with self._connect_ezsp(self.app_baudrate) as ezsp:
            (current_eui64,) = await ezsp.getEui64()
            _LOGGER.info("Current device IEEE: %s", current_eui64)

            if current_eui64 == new_eui64:
                _LOGGER.info("Device IEEE address already matches, not overwriting")
                return False

            if not await ezsp.can_write_custom_eui64():
                raise ValueError(
                    "IEEE address has already been written, it cannot be written again"
                )

            (status,) = await ezsp.setMfgToken(
                bellows.types.EzspMfgTokenId.MFG_CUSTOM_EUI_64, new_eui64.serialize()
            )

            if status != bellows.types.EmberStatus.SUCCESS:
                raise RuntimeError(f"Failed to write IEEE address: {status}")

        return True
