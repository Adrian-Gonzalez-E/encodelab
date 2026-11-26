# ffmpeg de apt (Ubuntu 26.04) no trae libvmaf: build estático GPL de BtbN, versión fijada.
FROM ubuntu:26.04 AS ffmpeg
# BtbN borra sus autobuilds al mes: si el original ya no está, se usa la copia de la release propia (mismo checksum).
ARG FFMPEG_FILE=ffmpeg-n8.1.3-41-ga8029b6528-linux64-gpl-8.1.tar.xz
ARG FFMPEG_SHA256=52e5d6c80ef213861dcdbdf5f742c551469ee7de91b3a846256660c9d0943997
ARG FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-11-25-12-41/${FFMPEG_FILE}
ARG FFMPEG_MIRROR=https://github.com/Adrian-Gonzalez-E/encodelab/releases/download/ffmpeg-n8.1.3-41/${FFMPEG_FILE}
RUN apt-get update && apt-get install -y --no-install-recommends xz-utils curl ca-certificates \
    && (curl -fsSL -o /tmp/ffmpeg.tar.xz "$FFMPEG_URL" || curl -fsSL -o /tmp/ffmpeg.tar.xz "$FFMPEG_MIRROR") \
    && echo "$FFMPEG_SHA256  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && mkdir /ffmpeg && tar -xJf /tmp/ffmpeg.tar.xz -C /ffmpeg --strip-components=1

FROM ubuntu:26.04
# iHD y el runtime VPL de Intel vienen de apt; el dispatcher VPL va dentro del ffmpeg estático.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3-flask gunicorn python3-pytest \
       intel-media-va-driver-non-free libmfx-gen1.2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /ffmpeg/bin/ffmpeg /ffmpeg/bin/ffprobe /usr/local/bin/
WORKDIR /app
COPY . /app
CMD ["gunicorn", "-w", "1", "--threads", "4", "--no-control-socket", "--timeout", "90", "-b", "0.0.0.0:8080", "encodelab.web:create_app()"]
