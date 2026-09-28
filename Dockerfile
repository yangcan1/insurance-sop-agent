FROM python:3.13-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY fixtures fixtures
ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
# PORT is overridable for hosts like Hugging Face Spaces (7860) or Render
CMD ["sh", "-c", "uvicorn app.server:app --host 0.0.0.0 --port ${PORT}"]
