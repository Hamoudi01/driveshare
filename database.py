"""
database.py — DriveShare Database Layer
CIS 476 Term Project

Uses SQLite (built into Python — no installation needed).
All tables, schema, and demo seed data are defined here.

Database Schema:
  users         — registered users (owners and renters)
  cars          — vehicle listings
  bookings      — rental reservations
  watchlist     — observer subscriptions (renter watching a car)
  notifications — observer notification messages
  messages      — in-app messaging between users
  reviews       — post-rental ratings and comments
  payment_log   — proxy audit trail for all payment attempts
"""

import sqlite3, os
from flask import g

DATABASE = os.path.join(os.path.dirname(__file__), 'instance', 'driveshare.db')


def get_db():
    """
    Return the database connection for the current request context.
    Uses Flask's 'g' object so the same connection is reused within
    one request and closed automatically when the request ends.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row   # rows behave like dicts (row['col'])
        g.db.execute("PRAGMA foreign_keys = ON")   # enforce FK constraints
    return g.db


def init_db():
    """
    Create all tables if they don't already exist, then seed demo data.
    Called once when the app starts (from app.py __main__ block).
    """
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # ── users ────────────────────────────────────────────────────────────────
    # Stores both owners and renters (any user can be either or both).
    # Passwords and security answers are stored as SHA-256 hashes.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL,
            email            TEXT    UNIQUE NOT NULL,
            password         TEXT    NOT NULL,          -- SHA-256 hash
            security_q1      TEXT    NOT NULL,          -- question text
            security_answer1 TEXT    NOT NULL,          -- SHA-256 hash
            security_q2      TEXT    NOT NULL,
            security_answer2 TEXT    NOT NULL,
            security_q3      TEXT    NOT NULL,
            security_answer3 TEXT    NOT NULL,
            balance          REAL    DEFAULT 500.00,    -- demo wallet balance
            created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── cars ─────────────────────────────────────────────────────────────────
    # Vehicle listings created by owners using the Builder pattern.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cars (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id      INTEGER NOT NULL REFERENCES users(id),
            make          TEXT    NOT NULL,   -- e.g. Toyota
            model         TEXT    NOT NULL,   -- e.g. Camry
            year          INTEGER NOT NULL,
            mileage       INTEGER DEFAULT 0,
            location      TEXT    NOT NULL,
            price_per_day REAL    NOT NULL,
            description   TEXT    DEFAULT '',
            image_url     TEXT    DEFAULT '',
            avail_start   DATE,               -- availability window start
            avail_end     DATE,               -- availability window end
            is_available  INTEGER DEFAULT 1,  -- 1=available, 0=unlisted
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── bookings ─────────────────────────────────────────────────────────────
    # Reservations created by renters. The app prevents overlapping bookings
    # for the same car via the search query (excludes already-booked cars).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id      INTEGER NOT NULL REFERENCES cars(id),
            renter_id   INTEGER NOT NULL REFERENCES users(id),
            start_date  DATE    NOT NULL,
            end_date    DATE    NOT NULL,
            total_price REAL    NOT NULL,
            status      TEXT    DEFAULT 'pending',  -- pending/approved/confirmed/cancelled
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── watchlist ─────────────────────────────────────────────────────────────
    # Observer pattern subscriptions: one row per (renter, car) pair.
    # max_price: renter gets notified when price drops to or below this value.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id     INTEGER NOT NULL REFERENCES cars(id),
            renter_id  INTEGER NOT NULL REFERENCES users(id),
            max_price  REAL,                          -- alert threshold (optional)
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(car_id, renter_id)                 -- no duplicate watches
        )
    """)

    # ── notifications ─────────────────────────────────────────────────────────
    # Observer pattern output: in-app alerts generated by WatchlistObserver.
    # Also used for booking approval/payment alerts.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            car_id     INTEGER REFERENCES cars(id),
            event_type TEXT    NOT NULL,   -- price_drop / booked / approved / payment
            message    TEXT    NOT NULL,
            is_read    INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── messages ─────────────────────────────────────────────────────────────
    # In-app messaging (Mediator MessageComponent writes here).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id   INTEGER NOT NULL REFERENCES users(id),
            receiver_id INTEGER NOT NULL REFERENCES users(id),
            content     TEXT    NOT NULL,
            is_read     INTEGER DEFAULT 0,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── reviews ───────────────────────────────────────────────────────────────
    # Post-rental ratings (1–5 stars) and comments by renters.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            car_id      INTEGER NOT NULL REFERENCES cars(id),
            reviewer_id INTEGER NOT NULL REFERENCES users(id),
            rating      INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            comment     TEXT    DEFAULT '',
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── payment_log ───────────────────────────────────────────────────────────
    # Proxy pattern audit log: every payment attempt is recorded here
    # whether it succeeds or fails.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payment_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            amount     REAL    NOT NULL,
            status     TEXT    DEFAULT 'attempted',  -- attempted/success/failed
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()

    # ── Seed demo data (only if tables are empty) ─────────────────────────────
    existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing == 0:
        import hashlib

        def h(s):
            return hashlib.sha256(s.encode()).hexdigest()

        # Demo users — password is "password123" for all
        users = [
            ("Alice Owner",  "alice@demo.com",  h("password123"),
             "What is your pet's name?",   h("fluffy"),
             "What city were you born in?", h("chicago"),
             "What is your mother's maiden name?", h("smith"), 1500.00),
            ("Bob Renter",   "bob@demo.com",    h("password123"),
             "What is your pet's name?",   h("rex"),
             "What city were you born in?", h("detroit"),
             "What is your mother's maiden name?", h("jones"),  800.00),
            ("Carol Driver", "carol@demo.com",  h("password123"),
             "What is your pet's name?",   h("whiskers"),
             "What city were you born in?", h("miami"),
             "What is your mother's maiden name?", h("brown"),  600.00),
        ]
        conn.executemany(
            """INSERT INTO users
               (name,email,password,security_q1,security_answer1,
                security_q2,security_answer2,security_q3,security_answer3,balance)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            users
        )

        # Demo cars — updated names and images per user specifications
        cars = [
            (1, "Ford", "Expedition", 2021, 25000, "Chicago, IL", 65.00,
             "Clean and reliable sedan. Perfect for city driving and commutes.",
             "https://images.unsplash.com/photo-1533473359331-0135ef1b58bf?w=600",
             "2026-04-01", "2026-12-31"),
            (1, "Nissan", "GT-R", 2022, 18000, "Austin, TX", 80.00,
             "Spacious SUV, great for road trips, families, and adventures.",
             "https://images.unsplash.com/photo-1568605117036-5fe5e7bab0b7?w=600",
             "2026-04-01", "2026-12-31"),
            (1, "Volkswagen", "Bugatti", 2020, 30000, "Miami, FL", 110.00,
             "Bold muscle car with a roaring V8 — turn heads everywhere you go.",
             "https://images.unsplash.com/photo-1544636331-e26879cd4d9b?w=600",
             "2026-04-01", "2026-12-31"),
            (3, "Tesla", "Model 3", 2023, 8000, "Seattle, WA", 120.00,
             "All-electric, autopilot-capable. Charging cable included.",
             "https://images.unsplash.com/photo-1560958089-b8a1929cea89?w=600",
             "2026-04-01", "2026-12-31"),
            (3, "Chevrolet", "Dodge Challenger", 2021, 22000, "New York, NY", 145.00,
             "Convertible sports car — thrilling open-air driving experience.",
             "https://images.unsplash.com/photo-1552519507-da3b142c6e3d?w=600",
             "2026-04-01", "2026-12-31"),
            (1, "Jeep", "Wrangler", 2021, 35000, "Denver, CO", 95.00,
             "Off-road ready 4x4. Perfect for mountain trails and outdoor adventures.",
             "https://media.ed.edmunds-media.com/jeep/wrangler/2025/oem/2025_jeep_wrangler_convertible-suv_rubicon-x_fq_oem_1_1600.jpg",
             "2026-04-01", "2026-12-31"),
            (3, "Chevrolet", "Camaro", 2022, 15000, "Las Vegas, NV", 115.00,
             "Iconic American muscle. Make your Vegas trip unforgettable.",
             "https://hips.hearstapps.com/hmg-prod/images/2024-chevrolet-camaro-ss-collectors-edition-1-647e1933c6c20.jpg?crop=0.755xw:0.696xh;0.112xw,0.149xh&resize=1200:*",
             "2026-04-01", "2026-12-31"),
            (1, "Ford", "F-150", 2022, 20000, "Dallas, TX", 85.00,
             "Full-size pickup truck. Great for hauling, moving, or road trips.",
             "https://www.ford-trucks.com/wp-content/uploads/2022/06/287606071_612198863806674_1489387876267703910_n.jpeg",
             "2026-04-01", "2026-12-31"),
            (3, "BMW", "X5", 2023, 12000, "Boston, MA", 130.00,
             "Luxury SUV with premium interior and smooth highway ride.",
             "https://images.unsplash.com/photo-1555215695-3004980ad54e?w=600",
             "2026-04-01", "2026-12-31"),
            (1, "Toyota", "Camry", 2020, 40000, "Phoenix, AZ", 55.00,
             "Affordable, fuel-efficient mid-size sedan. Great value for daily trips.",
             "https://images.unsplash.com/photo-1621007947382-bb3c3994e3fb?w=600",
             "2026-04-01", "2026-12-31"),
        ]
        conn.executemany(
            """INSERT INTO cars
               (owner_id,make,model,year,mileage,location,price_per_day,
                description,image_url,avail_start,avail_end)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            cars
        )
        conn.commit()

    conn.close()
