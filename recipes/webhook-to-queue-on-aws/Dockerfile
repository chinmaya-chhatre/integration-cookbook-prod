FROM python:3.12-slim
WORKDIR /app
COPY recipes/webhook-to-queue-on-aws/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY recipes/webhook-to-queue-on-aws/queue_recipe ./queue_recipe
COPY recipes/webhook-to-queue-on-aws/smoke.py ./
RUN useradd --uid 10001 --create-home queue-demo && mkdir /data && chown queue-demo /data
USER queue-demo
ENTRYPOINT ["python", "-m", "queue_recipe.runtime"]
CMD ["edge"]
