FROM python:3.12-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY quest_core ./quest_core
COPY questpb ./questpb
COPY grpc_service ./grpc_service
COPY app ./app

EXPOSE 8000 50051

# One process answers both: the app serves HTTP on 8000 and starts the gRPC server on 50051.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]