# ==============================================================================
# Production Dockerfile for NovaVideo on AWS Lambda
# ==============================================================================
FROM public.ecr.aws/lambda/python:3.11

# Install system utilities and static FFmpeg binary
RUN yum update -y && \
    yum install -y tar xz gzip wget && \
    wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
    tar -xf ffmpeg-release-amd64-static.tar.xz && \
    mv ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ffmpeg && \
    mv ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ffprobe && \
    chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe && \
    rm -rf ffmpeg-*-amd64-static* && \
    yum clean all

ENV FFMPEG_PATH=/usr/local/bin/ffmpeg
ENV STORAGE_DIR=/tmp/psp_converter

# Copy requirements and install dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy application files
COPY app.py dashboard.html psp-preset.json lambda_handler.py ${LAMBDA_TASK_ROOT}/

# Set Lambda function entrypoint
CMD ["lambda_handler.handler"]
