# The radio app, in a container.
#
# It is small on purpose: the program is Python standard library ONLY (no pip installs at all),
# and the two packages below are the command-line tools it drives — `v4l2-ctl` to tune the
# hardware and `parec`/`pactl` to capture audio and to route it to the speakers.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends v4l-utils pulseaudio-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY fm_radio.py /app/fm_radio.py

# Everything the program writes — saved stations, the mute flag, the last scan — goes here, so a
# mounted volume keeps it across a rebuild and nothing is written into the image.
ENV RADIO_DATA=/data \
    RADIO_PORT=8150 \
    RADIO_BIND=0.0.0.0 \
    RADIO_DEVICE=/dev/radio0 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8150

# 🔴 NOTHING HERE CAN PROVIDE THE DRIVER. Tuning needs the host kernel's V4L2 radio support for
# the stick (radio_shark/tea575x and friends), and a container cannot load a kernel module on a
# host it does not own. If /dev/radio0 does not exist on the host, this container cannot make it.
CMD ["python3", "/app/fm_radio.py"]
