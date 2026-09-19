from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AudioCodec(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AUDIO_CODEC_UNSPECIFIED: _ClassVar[AudioCodec]
    AUDIO_CODEC_PCM_S16LE: _ClassVar[AudioCodec]

class FinalizationReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FINALIZATION_REASON_UNSPECIFIED: _ClassVar[FinalizationReason]
    TRANSFER_SUCCESS: _ClassVar[FinalizationReason]
    CALL_ENDED: _ClassVar[FinalizationReason]
    TRANSFER_TIMEOUT: _ClassVar[FinalizationReason]
    TRANSFER_FAILED: _ClassVar[FinalizationReason]
    AGENT_DISCONNECTED: _ClassVar[FinalizationReason]
    SYSTEM_SHUTDOWN: _ClassVar[FinalizationReason]
AUDIO_CODEC_UNSPECIFIED: AudioCodec
AUDIO_CODEC_PCM_S16LE: AudioCodec
FINALIZATION_REASON_UNSPECIFIED: FinalizationReason
TRANSFER_SUCCESS: FinalizationReason
CALL_ENDED: FinalizationReason
TRANSFER_TIMEOUT: FinalizationReason
TRANSFER_FAILED: FinalizationReason
AGENT_DISCONNECTED: FinalizationReason
SYSTEM_SHUTDOWN: FinalizationReason

class GatewayMessage(_message.Message):
    __slots__ = ("session_open", "audio_chunk", "cancel_generation", "playback_finished", "speech_ended", "transfer_initiated", "transfer_completed", "transfer_failed", "dtmf")
    SESSION_OPEN_FIELD_NUMBER: _ClassVar[int]
    AUDIO_CHUNK_FIELD_NUMBER: _ClassVar[int]
    CANCEL_GENERATION_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_FINISHED_FIELD_NUMBER: _ClassVar[int]
    SPEECH_ENDED_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_INITIATED_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_FAILED_FIELD_NUMBER: _ClassVar[int]
    DTMF_FIELD_NUMBER: _ClassVar[int]
    session_open: SessionOpenRequest
    audio_chunk: AudioChunk
    cancel_generation: CancelGeneration
    playback_finished: PlaybackFinished
    speech_ended: SpeechEndedNotification
    transfer_initiated: TransferInitiated
    transfer_completed: TransferCompleted
    transfer_failed: TransferFailed
    dtmf: DtmfDigit
    def __init__(self, session_open: _Optional[_Union[SessionOpenRequest, _Mapping]] = ..., audio_chunk: _Optional[_Union[AudioChunk, _Mapping]] = ..., cancel_generation: _Optional[_Union[CancelGeneration, _Mapping]] = ..., playback_finished: _Optional[_Union[PlaybackFinished, _Mapping]] = ..., speech_ended: _Optional[_Union[SpeechEndedNotification, _Mapping]] = ..., transfer_initiated: _Optional[_Union[TransferInitiated, _Mapping]] = ..., transfer_completed: _Optional[_Union[TransferCompleted, _Mapping]] = ..., transfer_failed: _Optional[_Union[TransferFailed, _Mapping]] = ..., dtmf: _Optional[_Union[DtmfDigit, _Mapping]] = ...) -> None: ...

class DtmfDigit(_message.Message):
    __slots__ = ("session_id", "digit", "trace_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    DIGIT_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    digit: str
    trace_id: str
    def __init__(self, session_id: _Optional[str] = ..., digit: _Optional[str] = ..., trace_id: _Optional[str] = ...) -> None: ...

class SessionOpenRequest(_message.Message):
    __slots__ = ("protocol_version", "session_id", "tenant_id", "trace_id", "call_id", "caller_did", "called_did", "script_id", "codec", "sample_rate", "channels", "direction")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    CALLER_DID_FIELD_NUMBER: _ClassVar[int]
    CALLED_DID_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_ID_FIELD_NUMBER: _ClassVar[int]
    CODEC_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    CHANNELS_FIELD_NUMBER: _ClassVar[int]
    DIRECTION_FIELD_NUMBER: _ClassVar[int]
    protocol_version: str
    session_id: str
    tenant_id: str
    trace_id: str
    call_id: str
    caller_did: str
    called_did: str
    script_id: str
    codec: AudioCodec
    sample_rate: int
    channels: int
    direction: str
    def __init__(self, protocol_version: _Optional[str] = ..., session_id: _Optional[str] = ..., tenant_id: _Optional[str] = ..., trace_id: _Optional[str] = ..., call_id: _Optional[str] = ..., caller_did: _Optional[str] = ..., called_did: _Optional[str] = ..., script_id: _Optional[str] = ..., codec: _Optional[_Union[AudioCodec, str]] = ..., sample_rate: _Optional[int] = ..., channels: _Optional[int] = ..., direction: _Optional[str] = ...) -> None: ...

class AudioChunk(_message.Message):
    __slots__ = ("session_id", "trace_id", "sequence_num", "timestamp_us", "payload")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_NUM_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_US_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    trace_id: str
    sequence_num: int
    timestamp_us: int
    payload: bytes
    def __init__(self, session_id: _Optional[str] = ..., trace_id: _Optional[str] = ..., sequence_num: _Optional[int] = ..., timestamp_us: _Optional[int] = ..., payload: _Optional[bytes] = ...) -> None: ...

class CancelGeneration(_message.Message):
    __slots__ = ("session_id", "trace_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    trace_id: str
    def __init__(self, session_id: _Optional[str] = ..., trace_id: _Optional[str] = ...) -> None: ...

class PlaybackFinished(_message.Message):
    __slots__ = ("session_id", "trace_id", "interrupted")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    INTERRUPTED_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    trace_id: str
    interrupted: bool
    def __init__(self, session_id: _Optional[str] = ..., trace_id: _Optional[str] = ..., interrupted: _Optional[bool] = ...) -> None: ...

class SpeechEndedNotification(_message.Message):
    __slots__ = ("session_id", "trace_id", "duration_ms", "energy_db")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    ENERGY_DB_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    trace_id: str
    duration_ms: int
    energy_db: float
    def __init__(self, session_id: _Optional[str] = ..., trace_id: _Optional[str] = ..., duration_ms: _Optional[int] = ..., energy_db: _Optional[float] = ...) -> None: ...

class TransferInitiated(_message.Message):
    __slots__ = ("session_id", "transfer_type", "destination", "reason", "transfer_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_TYPE_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    transfer_type: str
    destination: str
    reason: str
    transfer_id: str
    def __init__(self, session_id: _Optional[str] = ..., transfer_type: _Optional[str] = ..., destination: _Optional[str] = ..., reason: _Optional[str] = ..., transfer_id: _Optional[str] = ...) -> None: ...

class TransferCompleted(_message.Message):
    __slots__ = ("session_id", "destination", "transfer_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    destination: str
    transfer_id: str
    def __init__(self, session_id: _Optional[str] = ..., destination: _Optional[str] = ..., transfer_id: _Optional[str] = ...) -> None: ...

class TransferFailed(_message.Message):
    __slots__ = ("session_id", "destination", "reason", "transfer_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    destination: str
    reason: str
    transfer_id: str
    def __init__(self, session_id: _Optional[str] = ..., destination: _Optional[str] = ..., reason: _Optional[str] = ..., transfer_id: _Optional[str] = ...) -> None: ...

class ServiceMessage(_message.Message):
    __slots__ = ("service_ready", "tts_chunk", "cancel_ack", "error", "stt_result", "tts_started", "end_call", "transfer_request", "conversation_finalized")
    SERVICE_READY_FIELD_NUMBER: _ClassVar[int]
    TTS_CHUNK_FIELD_NUMBER: _ClassVar[int]
    CANCEL_ACK_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    STT_RESULT_FIELD_NUMBER: _ClassVar[int]
    TTS_STARTED_FIELD_NUMBER: _ClassVar[int]
    END_CALL_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_REQUEST_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_FINALIZED_FIELD_NUMBER: _ClassVar[int]
    service_ready: ServiceReady
    tts_chunk: TtsChunk
    cancel_ack: CancelAck
    error: ServiceError
    stt_result: SttResult
    tts_started: TtsStarted
    end_call: EndCall
    transfer_request: TransferRequest
    conversation_finalized: ConversationFinalized
    def __init__(self, service_ready: _Optional[_Union[ServiceReady, _Mapping]] = ..., tts_chunk: _Optional[_Union[TtsChunk, _Mapping]] = ..., cancel_ack: _Optional[_Union[CancelAck, _Mapping]] = ..., error: _Optional[_Union[ServiceError, _Mapping]] = ..., stt_result: _Optional[_Union[SttResult, _Mapping]] = ..., tts_started: _Optional[_Union[TtsStarted, _Mapping]] = ..., end_call: _Optional[_Union[EndCall, _Mapping]] = ..., transfer_request: _Optional[_Union[TransferRequest, _Mapping]] = ..., conversation_finalized: _Optional[_Union[ConversationFinalized, _Mapping]] = ...) -> None: ...

class ServiceReady(_message.Message):
    __slots__ = ("session_id", "protocol_version")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    protocol_version: str
    def __init__(self, session_id: _Optional[str] = ..., protocol_version: _Optional[str] = ...) -> None: ...

class SttResult(_message.Message):
    __slots__ = ("session_id", "trace_id", "text", "confidence", "is_final")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    IS_FINAL_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    trace_id: str
    text: str
    confidence: float
    is_final: bool
    def __init__(self, session_id: _Optional[str] = ..., trace_id: _Optional[str] = ..., text: _Optional[str] = ..., confidence: _Optional[float] = ..., is_final: _Optional[bool] = ...) -> None: ...

class TtsStarted(_message.Message):
    __slots__ = ("session_id", "trace_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    trace_id: str
    def __init__(self, session_id: _Optional[str] = ..., trace_id: _Optional[str] = ...) -> None: ...

class TtsChunk(_message.Message):
    __slots__ = ("session_id", "trace_id", "sequence_num", "codec", "sample_rate", "payload", "is_final")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_NUM_FIELD_NUMBER: _ClassVar[int]
    CODEC_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    IS_FINAL_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    trace_id: str
    sequence_num: int
    codec: AudioCodec
    sample_rate: int
    payload: bytes
    is_final: bool
    def __init__(self, session_id: _Optional[str] = ..., trace_id: _Optional[str] = ..., sequence_num: _Optional[int] = ..., codec: _Optional[_Union[AudioCodec, str]] = ..., sample_rate: _Optional[int] = ..., payload: _Optional[bytes] = ..., is_final: _Optional[bool] = ...) -> None: ...

class CancelAck(_message.Message):
    __slots__ = ("session_id", "trace_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    trace_id: str
    def __init__(self, session_id: _Optional[str] = ..., trace_id: _Optional[str] = ...) -> None: ...

class ServiceError(_message.Message):
    __slots__ = ("session_id", "code", "message", "fatal")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    FATAL_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    code: str
    message: str
    fatal: bool
    def __init__(self, session_id: _Optional[str] = ..., code: _Optional[str] = ..., message: _Optional[str] = ..., fatal: _Optional[bool] = ...) -> None: ...

class EndCall(_message.Message):
    __slots__ = ("session_id", "reason", "grace_period_ms")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    GRACE_PERIOD_MS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    reason: str
    grace_period_ms: int
    def __init__(self, session_id: _Optional[str] = ..., reason: _Optional[str] = ..., grace_period_ms: _Optional[int] = ...) -> None: ...

class TransferRequest(_message.Message):
    __slots__ = ("session_id", "transfer_type", "destination", "reason", "transfer_id", "caller_id", "waiting_experience")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_TYPE_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    TRANSFER_ID_FIELD_NUMBER: _ClassVar[int]
    CALLER_ID_FIELD_NUMBER: _ClassVar[int]
    WAITING_EXPERIENCE_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    transfer_type: str
    destination: str
    reason: str
    transfer_id: str
    caller_id: str
    waiting_experience: str
    def __init__(self, session_id: _Optional[str] = ..., transfer_type: _Optional[str] = ..., destination: _Optional[str] = ..., reason: _Optional[str] = ..., transfer_id: _Optional[str] = ..., caller_id: _Optional[str] = ..., waiting_experience: _Optional[str] = ...) -> None: ...

class ConversationFinalized(_message.Message):
    __slots__ = ("session_id", "reason", "summary_generated", "transcript_written")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_GENERATED_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_WRITTEN_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    reason: FinalizationReason
    summary_generated: bool
    transcript_written: bool
    def __init__(self, session_id: _Optional[str] = ..., reason: _Optional[_Union[FinalizationReason, str]] = ..., summary_generated: _Optional[bool] = ..., transcript_written: _Optional[bool] = ...) -> None: ...
