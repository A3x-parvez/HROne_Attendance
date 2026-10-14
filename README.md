# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## API coverage

The FastAPI app implements the paths and response shapes in `openapi.yaml`, including:

- Employee creation and listing.
- Attendance punch-in, punch-out, listing, and audited regularization.
- Employee monthly analytics, department summaries, the late leaderboard, and daily department trends.
- MongoDB execution plans at `/admin/explain/{endpoint}` for the five supported endpoint names.

Analytics are aggregated in MongoDB. The department trend uses `$densify` and a seven-row window so dates with no
attendance records are still returned. Startup creates unique employee and attendance keys plus date and department
indexes for the filtered queries and aggregations. The explain endpoint runs the corresponding query or aggregation with
`executionStats` verbosity.

## Submit

A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
