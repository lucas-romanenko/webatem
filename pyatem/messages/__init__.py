"""
ATEM wire-format messages — declarative DSL classes, one file per feature.

Every Send (outgoing) and Recv (incoming) class is declared in a per-feature
module; this package's ``__init__`` re-exports them all so callers can use
either form:

    from pyatem.messages.switching import CutCommand
    from pyatem.messages import CutCommand

The ``RECV_BY_CODE`` mapping is built once at import time by walking the
``Recv`` subclasses; ``pyatem.protocol.AtemProtocol`` uses it to route
incoming packets to the right parser by 4-char wire code.
"""

from pyatem.messages._dsl import Field, Recv, Send

# --- Send classes ----------------------------------------------------------
from pyatem.messages.color_generator import ColorGeneratorCommand
from pyatem.messages.downstream_keyer import (
    DkeyAutoCommand,
    DkeyGainCommand,
    DkeyMaskCommand,
    DkeyOnairCommand,
    DkeyRateCommand,
    DkeySetFillCommand,
    DkeySetKeyCommand,
    DkeyTieCommand,
)
from pyatem.messages.fade_to_black import (
    FadeToBlackCommand,
    FadeToBlackConfigCommand,
    FadeToBlackEnableCommand,
)
from pyatem.messages.fairlight import (
    FairlightCompressorPropertiesCommand,
    FairlightEqBandPropertiesCommand,
    FairlightExpanderPropertiesCommand,
    FairlightLimiterPropertiesCommand,
    FairlightMasterCompressorPropertiesCommand,
    FairlightMasterEqBandPropertiesCommand,
    FairlightMasterLimiterPropertiesCommand,
    FairlightMasterPropertiesCommand,
    FairlightStripPropertiesCommand,
    SendFairlightLevelsCommand,
)
from pyatem.messages.file_transfer import (
    TransferAckCommand,
    TransferDataCommand,
    TransferDownloadRequestCommand,
    TransferFileDataCommand,
    TransferUploadRequestCommand,
)
from pyatem.messages.input_video import (
    InputPropertiesCommand,
    VideoModeCommand,
)
from pyatem.messages.lock import (
    LockCommand,
    PartialLockCommand,
)
from pyatem.messages.macros import (
    MacroActionCommand,
    MacroRecordCommand,
    MacroSleepCommand,
)
from pyatem.messages.media import (
    CaptureStillCommand,
    ClearStillCommand,
    MediaplayerSelectCommand,
)
from pyatem.messages.switching import (
    AutoCommand,
    AuxSourceCommand,
    CutCommand,
    PreviewInputCommand,
    ProgramInputCommand,
)
# (TimeRequestCommand merged into system_info.py — see import block above.)
from pyatem.messages.transition import (
    DipSettingsCommand,
    DveSettingsCommand,
    MixSettingsCommand,
    StingerSettingsCommand,
    TransitionSettingsCommand,
    WipeSettingsCommand,
)
from pyatem.messages.upstream_keyer import (
    KeyCutCommand,
    KeyFillCommand,
    KeyOnAirCommand,
    KeyPropertiesAdvancedChromaColorpickerCommand,
    KeyPropertiesAdvancedChromaCommand,
    KeyPropertiesDveCommand,
    KeyPropertiesLumaCommand,
    KeyPropertiesMaskCommand,
    KeyPropertiesPatternCommand,
    KeyTypeCommand,
    KeyerKeyframeRunCommand,
    KeyerKeyframeSetCommand,
)
# (KeyOnAirCommand merged into upstream_keyer.py — see import block above.)

# --- Recv classes ----------------------------------------------------------
from pyatem.messages.audio_legacy import (
    AtemEqBandPropertiesField,
    AtemMasterEqBandPropertiesField,
    AudioInputField,
    AudioMixerMasterPropertiesField,
    AudioMixerMonitorPropertiesField,
    AudioMixerTallyField,
)
from pyatem.messages.camera_control import CameraControlDataPacketField
from pyatem.messages.color_generator import ColorGeneratorField
# (AutoInputVideoModeField / InitCompleteField / TransferCompleteField
# now live in system_info.py — see the system_info import block below.)
from pyatem.messages.downstream_keyer import (
    DkeyPropertiesBaseField,
    DkeyPropertiesField,
    DkeyStateField,
)
from pyatem.messages.fade_to_black import (
    FadeToBlackEnabledField,
    FadeToBlackField,
    FadeToBlackStateField,
)
from pyatem.messages.fairlight import (
    FairlightAudioInputField,
    FairlightCompressorPropertiesField,
    FairlightExpanderPropertiesField,
    FairlightHeadphonesField,
    FairlightLimiterPropertiesField,
    FairlightMasterCompressorPropertiesField,
    FairlightMasterLimiterPropertiesField,
    FairlightMasterPropertiesField,
    FairlightSoloField,
    FairlightStripDeleteField,
    FairlightStripPropertiesField,
    FairlightTallyField,
)
from pyatem.messages.file_transfer import (
    FileTransferContinueDataField,
    FileTransferDataCompleteField,
    FileTransferDataField,
    FileTransferErrorField,
)
from pyatem.messages.hyperdeck import HyperdeckSettingsField
from pyatem.messages.input_video import (
    InputPropertiesField,
    VideoModeCapabilityField,
    VideoModeField,
)
from pyatem.messages.lock import (
    LockObtainedField,
    LockStateField,
)
from pyatem.messages.macros import (
    MacroPlayStatusField,
    MacroPropertiesField,
    MacroRecordStatusField,
)
from pyatem.messages.manual import ManualField
from pyatem.messages.media import MediaplayerFileInfoField
from pyatem.messages.meters import (
    AudioMeterLevelsField,
    FairlightMasterLevelsField,
    FairlightMeterLevelsField,
)
from pyatem.messages.multiviewer import (
    MultiviewerInputField,
    MultiviewerPropertiesField,
    MultiviewerSafeAreaField,
    MultiviewerVuField,
)
from pyatem.messages.recording import (
    RecordingDiskField,
    RecordingDurationField,
    RecordingSettingsField,
    RecordingStatusField,
)
from pyatem.messages.streaming import (
    StreamingAudioBitrateField,
    StreamingServiceField,
    StreamingStatsField,
    StreamingStatusField,
)
from pyatem.messages.supersource import (
    SupersourceBoxPropertiesField,
    SupersourcePropertiesField,
)
from pyatem.messages.switching import (
    AuxOutputSourceField,
    PreviewBusInputField,
    ProgramBusInputField,
)
from pyatem.messages.system_info import (
    AutoInputVideoModeField,
    FirmwareVersionField,
    InitCompleteField,
    MediaplayerSelectedField,
    MediaplayerSlotsField,
    MixerEffectConfigField,
    ProductNameField,
    Sdi3GLevelField,
    TimeConfigField,
    TimeField,
    TimeRequestCommand,
    TopologyField,
    TransferCompleteField,
)
from pyatem.messages.tally import (
    TallyIndexField,
    TallySourceField,
)
# (TopologyField merged into system_info.py — see import block above.)
from pyatem.messages.transition import (
    TransitionDipField,
    TransitionDveField,
    TransitionMixField,
    TransitionPositionField,
    TransitionPreviewField,
    TransitionSettingsField,
    TransitionStingerField,
    TransitionWipeField,
)
from pyatem.messages.upstream_keyer import (
    KeyOnAirField,
    KeyPropertiesAdvancedChromaColorpickerField,
    KeyPropertiesAdvancedChromaField,
    KeyPropertiesBaseField,
    KeyPropertiesDveField,
    KeyPropertiesFlyField,
    KeyPropertiesFlyKeyframeField,
    KeyPropertiesLumaField,
    KeyPropertiesPatternField,
)


def _build_recv_registries():
    """Walk every Recv subclass at module-import time and build the
    three lookup tables protocol.py needs:

    - ``RECV_BY_CODE``     — 4-char wire code → Recv class
    - ``PRETTY_BY_CODE``   — 4-char wire code → pretty dict-key name
    - ``KEY_FORMAT_BY_PRETTY`` — pretty name → struct.Struct (for indexed
                                  fields like per-M/E or per-strip state)

    ``ManualField`` is excluded — it's a catch-all whose CODE is set
    per-instance, not class-level.
    """
    by_code = {}
    pretty_by_code = {}
    key_format_by_pretty = {}
    seen = set()

    def visit(cls):
        if cls in seen:
            return
        seen.add(cls)
        for sub in cls.__subclasses__():
            visit(sub)
            code = getattr(sub, 'CODE', '')
            if not code or sub is ManualField:
                continue
            by_code[code] = sub
            pretty = getattr(sub, 'PRETTY', '')
            if pretty:
                pretty_by_code[code] = pretty
                kf = getattr(sub, 'KEY_FORMAT', None)
                if kf is not None:
                    key_format_by_pretty[pretty] = kf

    visit(Recv)
    return by_code, pretty_by_code, key_format_by_pretty


RECV_BY_CODE, PRETTY_BY_CODE, KEY_FORMAT_BY_PRETTY = _build_recv_registries()
