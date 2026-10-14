# Employee Attendance & Analytics API

## Overview

A RESTful API built using FastAPI and MongoDB to manage employee records, attendance tracking, punch-in and punch-out operations, attendance regularization, and workforce analytics.

## Features

- Employee creation and listing
- Employee punch-in and punch-out
- Attendance records with filtering and pagination
- Attendance regularization with history tracking
- Monthly employee attendance analytics
- Department attendance summaries and trends
- Late-arrival leaderboard
- MongoDB indexes for data integrity and query performance
- Input validation and attendance rule calculations

## Technologies

- Python
- FastAPI
- MongoDB Atlas
- PyMongo
- Uvicorn
- Pytest

## Setup

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Configure environment variables

Create a local `.env` file and configure:

```env
MONGO_URI="your_mongodb_connection_string"
MONGO_DB="hrone_attendance"
```

Use your own MongoDB connection string. Never commit database credentials to version control.

### 4. Load sample data (optional)

```bash
python sample_seed.py
```

Run this only when sample data is needed.

## Run the API

```bash
uvicorn app.main:app --reload --port 8000
```

## API Documentation

Once the application is running:

- **Swagger UI:** http://127.0.0.1:8000/docs
- **Health Check:** http://127.0.0.1:8000/health

## Run Tests

Run the complete test suite:

```bash
python -m pytest tests/ -v
```

The supplied test suites currently pass all 87 tests in the local environment.

## Project Files

- `app/main.py` — API implementation
- `requirements.txt` — Project dependencies
- `sample_seed.py` — Sample data setup
- `tests/` — Automated tests
- `REVIEW.md` — Review notes
- `DECISIONS.md` — Implementation decisions
- `README.md` — Project documentation

## Security

- Keep `.env` out of version control.
- Do not commit MongoDB credentials or other secrets.
- Restrict MongoDB Atlas network access to trusted IP addresses.