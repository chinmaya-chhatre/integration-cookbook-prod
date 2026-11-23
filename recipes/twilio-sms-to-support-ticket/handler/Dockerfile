FROM python:3.12-slim
WORKDIR /app
COPY recipes/twilio-sms-to-support-ticket/*.py ./
RUN useradd --uid 10001 --create-home sms && mkdir /data && chown sms /data
USER sms
ENV TICKET_STATE_DB=/data/tickets.sqlite3
EXPOSE 8080
CMD ["python", "runtime.py"]
