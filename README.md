# encodelab

Pruebas de codificación (AV1/HEVC, CPU o Intel QSV) con VMAF, PSNR y SSIM para elegir ajustes de transcodificación.
Flask + SQLite, ffmpeg estático con libvmaf. Pensado para correr en Docker detrás de un proxy con autenticación: la app no tiene login propio.

## Requisitos
- Docker Compose; GPU Intel Arc opcional (QSV) vía `/dev/dri`.
- Un directorio de medios montado en solo lectura en `/media`.

## Uso
Ajusta en `compose.yaml` los devices, el montaje de medios y la URL de `x-dockge`, y:

    docker compose up -d --build

Tests: `docker compose run --rm --no-deps encodelab python3 -m pytest -q -p no:cacheprovider tests`
E2E de interfaz: `app/e2e/run.sh [directorio de capturas]`.

## Seguridad
Sin puertos publicados: exponla solo tras un proxy con autenticación. Los POST validan `Sec-Fetch-Site`.

## Licencia
AGPL-3.0, ver [LICENSE](LICENSE). Si ofreces una versión modificada como servicio, debes publicar su código.
