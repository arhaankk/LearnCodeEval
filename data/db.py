import os

import mysql.connector
from dotenv import load_dotenv

load_dotenv()


def get_connection() -> mysql.connector.MySQLConnection:
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )


if __name__ == "__main__":
    connection = get_connection()
    try:
        print("Connected to LearnCoding!")

        cursor = connection.cursor(dictionary=True)
        cursor.execute("SHOW TABLES")

        for row in cursor.fetchall():
            print(row)

        cursor.close()
    finally:
        connection.close()
