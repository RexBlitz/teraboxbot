# ===== Base Image =====
FROM python:3.11-slim

# ===== Set Workdir =====
WORKDIR /app

# ===== Copy Requirements =====
COPY requirements.txt .

# ===== Install Dependencies =====
RUN pip install --no-cache-dir -r requirements.txt

# ===== Copy Bot Code =====
COPY . .


# ===== Run Bot =====
CMD ["python", "main.py"]
