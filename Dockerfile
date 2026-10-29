FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir . && useradd -u 10001 -m kdoctor
USER 10001
ENTRYPOINT ["kdoctor"]
CMD ["scan"]
