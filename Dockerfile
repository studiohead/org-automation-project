# PLAIN ENGLISH: A recipe for packaging the app into a self-contained "box" (container)
# so it runs the same on any machine. Build: docker build -t org-automation .
# Run:   docker run -p 8000:8000 --env-file .env org-automation
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[claude]"
# Don't run as the all-powerful "root" user inside the box.
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "org_automation.main:app", "--host", "0.0.0.0", "--port", "8000"]
