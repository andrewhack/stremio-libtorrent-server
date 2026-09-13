"""WebAdmin extension for the fork-owned FFmpeg transcoding policy.

This module deliberately extends the existing WebAdmin at startup instead of
modifying its core application. The generic Configuration page automatically
renders these settings and persists them to admin-settings.json, which is also
read by the FFmpeg wrapper in the streaming container.
"""
from __future__ import annotations

import app as legacy

TRANSCODING_DESCRIPTIONS = {
    "transcoding_mode": (
        "Política FFmpeg: auto preserva Direct Stream quando compatível; copy desactiva alterações; "
        "h264/hevc/software força transcodificação apenas onde o core pediu stream copy."
    ),
    "transcoding_hwaccel": "Aceleração preferida: vaapi, nvenc, cpu ou auto.",
    "transcoding_vaapi_device": "Render node VAAPI utilizado pelo wrapper FFmpeg.",
    "transcoding_video_codec": "Encoder de vídeo preferido no modo auto, por exemplo h264_vaapi.",
    "transcoding_video_quality": "Qualidade do vídeo (0-51); 22 é o valor recomendado por defeito.",
    "transcoding_audio_codec": "Codec usado quando o áudio necessita de conversão; AAC é o padrão.",
    "transcoding_audio_bitrate": "Bitrate do áudio convertido, por exemplo 192k.",
    "transcoding_copy_video": "Mantém cópia directa de vídeo quando o codec está na lista compatível.",
    "transcoding_copy_audio": "Mantém cópia directa de áudio quando o codec está na lista compatível.",
    "transcoding_direct_video_codecs": "Codecs de vídeo permitidos para Direct Stream, separados por vírgula.",
    "transcoding_direct_audio_codecs": "Codecs de áudio permitidos para Direct Stream, separados por vírgula.",
    "transcoding_fallback_codec": "Encoder software utilizado quando a aceleração por hardware não está disponível.",
    "transcoding_hw_decode": "Usa descodificação por hardware juntamente com o encoder por hardware.",
}

TRANSCODING_DEFAULTS = {
    "transcoding_mode": "auto",
    "transcoding_hwaccel": "vaapi",
    "transcoding_vaapi_device": "/dev/dri/renderD128",
    "transcoding_video_codec": "h264_vaapi",
    "transcoding_video_quality": 22,
    "transcoding_audio_codec": "aac",
    "transcoding_audio_bitrate": "192k",
    "transcoding_copy_video": True,
    "transcoding_copy_audio": True,
    "transcoding_direct_video_codecs": "h264",
    "transcoding_direct_audio_codecs": "aac,mp3,ac3",
    "transcoding_fallback_codec": "libx264",
    "transcoding_hw_decode": True,
}

legacy.DESCRIPTIONS.update(TRANSCODING_DESCRIPTIONS)
legacy.DEFAULTS.update(TRANSCODING_DEFAULTS)

# Import only after patching the shared legacy module. version_lifecycle ->
# fork_update -> app reuses this same module object from sys.modules.
from version_lifecycle import app  # noqa: E402,F401
