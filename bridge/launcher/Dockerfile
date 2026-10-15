FROM debian:13

ENV DEBIAN_FRONTEND=noninteractive
ENV LIBGL_ALWAYS_SOFTWARE=1
ENV DISPLAY=:99

# 校园网直连 deb.debian.org 不稳定。
RUN sed -i "s|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g" /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb x11-utils mesa-utils libgl1-mesa-dri libglx-mesa0 \
      openjdk-21-jre-headless \
      imagemagick xdotool ffmpeg \
      python3 ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /data
CMD ["bash"]
