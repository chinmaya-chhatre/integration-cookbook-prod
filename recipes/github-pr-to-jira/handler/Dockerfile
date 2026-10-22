FROM python:3.12-slim
WORKDIR /app
COPY app.py .
# The mapping database lives on the compose volume; the volume takes its owner from this folder.
RUN mkdir /data && chown nobody /data
VOLUME /data
EXPOSE 8080
USER nobody
CMD ["python", "app.py"]
