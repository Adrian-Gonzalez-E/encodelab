# ffmpeg de apt (Ubuntu 26.04) no trae libvmaf: build estático GPL de BtbN, versión fijada.
FROM ubuntu:26.04 AS ffmpeg
ADD --checksum=sha256:cc546852e535193e6d30a1f18cb90daa84e8ff320ceac6c6f5b514a1fba67079 \
    https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-19-13-06/ffmpeg-n8.1.3-18-g46d899d7a0-linux64-gpl-8.1.tar.xz \
    /tmp/ffmpeg.tar.xz
RUN apt-get update && apt-get install -y --no-install-recommends xz-utils \
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
CMD ["gunicorn", "-w", "1", "--threads", "4", "--no-control-socket", "-b", "0.0.0.0:8080", "encodelab.web:create_app()"]
