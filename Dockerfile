FROM python
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    yt-dlp \
    && rm -rf /var/lib/apt/lists/*
VOLUME /downloads
WORKDIR /downloads
COPY video.py /video.py
RUN export SRC_URL=deps://install; \
    export OUT_FILE=/downloads/video.mp4; \
    python /video.py

ENTRYPOINT ["python"]
CMD ["/video.py"]
