FROM python:3.12-slim
WORKDIR /app
RUN mkdir /data && chown nobody:nogroup /data
COPY app.py .
EXPOSE 8080
USER nobody
CMD ["python", "app.py"]
