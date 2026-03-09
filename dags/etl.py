"""
NASA APOD ETL Pipeline
======================
Extracts Astronomy Picture of the Day data from NASA's API,
transforms the JSON payload, and loads into PostgreSQL with
idempotent inserts (safe for backfills and re-runs).

Schedule: Daily at 6:00 AM UTC
Catchup:  Enabled (supports historical backfill)
"""

from datetime import datetime, timedelta

from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator
from airflow.decorators import task
from airflow.providers.postgres.hooks.postgres import PostgresHook


default_args = {
    "owner": "vatsal",
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "execution_timeout": timedelta(minutes=10),
}

with DAG(
    dag_id="nasa_apod_postgres",
    default_args=default_args,
    description="ETL pipeline: NASA APOD API → Transform → PostgreSQL",
    schedule="@daily",
    start_date=datetime(2024, 1, 1),
    catchup=True,
    max_active_runs=3,
    tags=["etl", "nasa", "postgres"],
) as dag:


    # Step 1: Create table with UNIQUE constraint on date for idempotency

    @task
    def create_table():
        """Create the APOD target table if it doesn't exist."""
        postgres_hook = PostgresHook(postgres_conn_id="my_postgres_connection")
        create_table_query = """
        CREATE TABLE IF NOT EXISTS apod_data (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255),
            explanation TEXT,
            url TEXT,
            date DATE UNIQUE,
            media_type VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        postgres_hook.run(create_table_query)


    # Step 2: Extract — fetch APOD data from NASA API

    extract_apod = HttpOperator(
        task_id="extract_apod",
        http_conn_id="nasa_api",
        endpoint="planetary/apod",
        method="GET",
        data={
            "api_key": "{{ conn.nasa_api.extra_dejson.api_key }}",
            "date": "{{ ds }}",  # logical date for backfill support
        },
        response_filter=lambda response: response.json(),
    )


    # Step 3: Transform — validate and extract required fields

    @task
    def transform_apod_data(response: dict) -> dict:
        """Parse API response and validate required fields."""
        required_fields = ["title", "date", "url"]
        for field in required_fields:
            if not response.get(field):
                raise ValueError(f"Missing required field: {field}")

        return {
            "title": response["title"].strip(),
            "explanation": response.get("explanation", "").strip(),
            "url": response["url"].strip(),
            "date": response["date"],
            "media_type": response.get("media_type", "unknown"),
        }


    # Step 4: Load — idempotent insert (skip duplicates on same date)
    @task
    def load_data_to_postgres(apod_data: dict):
        """Insert record into PostgreSQL. Skips if date already exists."""
        postgres_hook = PostgresHook(postgres_conn_id="my_postgres_connection")
        insert_query = """
        INSERT INTO apod_data (title, explanation, url, date, media_type)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (date) DO NOTHING;
        """
        postgres_hook.run(
            insert_query,
            parameters=(
                apod_data["title"],
                apod_data["explanation"],
                apod_data["url"],
                apod_data["date"],
                apod_data["media_type"],
            ),
        )


    create_table() >> extract_apod
    api_response = extract_apod.output
    transformed_data = transform_apod_data(api_response)
    load_data_to_postgres(transformed_data)
