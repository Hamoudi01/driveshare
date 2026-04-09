"""
DriveShare - Peer-to-Peer Car Rental Platform
CIS 476 Term Project

Author: [Your Name]
Date: Spring 2026

Design patterns implemented:
  - Singleton              : UserSession (secure session management)
  - Observer               : WatchList notifications (price/availability alerts)
  - Mediator               : UIMediator (coordinates UI component communication)
  - Builder                : CarListingBuilder (constructs car listing objects)
  - Proxy                  : PaymentProxy (simulates secure payment gateway)
  - Chain of Responsibility: PasswordRecovery via 3 security questions

FIXES v2:
  - Edit car now includes image_url field and cancel/delist option
  - Search fixed (corrected date-overlap SQL logic)
  - Booking prevents duplicate overlapping reservations
  - Payment actually deducts wallet and notifies BOTH owner and renter
  - Messaging uses name dropdown instead of raw ID (no more IntegrityError)
  - Messages page shows sent + received conversations
  - Booking cancellation added
  - Balance refreshed from DB on every dashboard load
  - Fixed secret key so sessions survive server restart
"""

from flask import Flask, render_template, request, redirect, url_for, session, flash
from database import init_db, get_db
import hashlib, os
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# Flask App Setup
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
# Fixed key — sessions persist across server restarts (os.urandom resets on restart)
app.secret_key = 'driveshare-cis476-secret-key-2026'


# ─────────────────────────────────────────────────────────────────────────────
# PATTERN 1: SINGLETON — UserSession
#
# Role mapping:
#   Singleton class  → UserSession
#   Managed resource → The single shared Flask session state object
#
# Ensures only one session manager exists throughout the app lifecycle.
# Centralises all session read/write so no component bypasses auth checks.
# ─────────────────────────────────────────────────────────────────────────────
class UserSession:
    """
    Singleton — only one instance ever created.
    All auth operations go through UserSession.get_instance().
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls):
        """Return the single UserSession instance, creating it if needed."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def login(self, user):
        """Write user info into Flask's server-side session."""
        session['user_id']    = user['id']
        session['user_email'] = user['email']
        session['user_name']  = user['name']

    def logout(self):
        session.clear()

    def get_current_user(self):
        if 'user_id' in session:
            return {'id': session['user_id'],
                    'email': session['user_email'],
                    'name': session['user_name']}
        return None

    def is_logged_in(self):
        return 'user_id' in session


# ─────────────────────────────────────────────────────────────────────────────
# PATTERN 2: OBSERVER — Watchlist Notifications
#
# Role mapping:
#   Subject (Observable) → CarListingSubject
#   Observer interface   → RenterObserver  (abstract base)
#   Concrete Observer    → WatchlistObserver (one per watching renter)
#   Notification store   → notifications table in SQLite
# ─────────────────────────────────────────────────────────────────────────────
class RenterObserver:
    """Abstract Observer — subclasses must implement notify()."""
    def notify(self, car_id, event_type, message):
        raise NotImplementedError


class WatchlistObserver(RenterObserver):
    """
    Concrete Observer attached to one renter.
    When notified, writes a row to the notifications table.
    """
    def __init__(self, renter_id):
        self.renter_id = renter_id

    def notify(self, car_id, event_type, message):
        db = get_db()
        db.execute(
            "INSERT INTO notifications (user_id, car_id, event_type, message) "
            "VALUES (?,?,?,?)",
            (self.renter_id, car_id, event_type, message)
        )
        db.commit()


class CarListingSubject:
    """
    Subject — holds observer list and broadcasts to all registered observers.
    """
    def __init__(self, car_id):
        self.car_id     = car_id
        self._observers = []

    def attach(self, observer: RenterObserver):
        self._observers.append(observer)

    def detach(self, observer: RenterObserver):
        self._observers.remove(observer)

    def notify_observers(self, event_type, message):
        for obs in self._observers:
            obs.notify(self.car_id, event_type, message)


def notify_watchers(car_id, event_type, message):
    """
    Helper used by routes to fire Observer notifications.
    Loads all watchers from the DB, attaches them as observers, then broadcasts.
    """
    db = get_db()
    watchers = db.execute(
        "SELECT renter_id FROM watchlist WHERE car_id=?", (car_id,)
    ).fetchall()
    subject = CarListingSubject(car_id)
    for w in watchers:
        subject.attach(WatchlistObserver(w['renter_id']))
    subject.notify_observers(event_type, message)


# ─────────────────────────────────────────────────────────────────────────────
# PATTERN 3: MEDIATOR — UIMediator
#
# Role mapping:
#   Mediator interface → UIMediator (abstract)
#   Concrete Mediator  → DriveShareMediator
#   Colleague classes  → SearchComponent, BookingComponent, MessageComponent
#
# Components communicate through the mediator, never directly with each other.
# ─────────────────────────────────────────────────────────────────────────────
class UIMediator:
    """Abstract Mediator."""
    def notify(self, sender, event, data=None):
        raise NotImplementedError


class DriveShareMediator(UIMediator):
    """Concrete Mediator — wires together Search, Booking, and Message colleagues."""
    def __init__(self):
        self.search   = SearchComponent(self)
        self.booking  = BookingComponent(self)
        self.messages = MessageComponent(self)

    def notify(self, sender, event, data=None):
        if event == 'search_executed':
            return self.booking.reset(data)
        elif event == 'booking_confirmed':
            self.messages.send_booking_confirmation(data)
            notify_watchers(data['car_id'], 'booked',
                            'A car you are watching was just booked.')
        elif event == 'message_sent':
            return self.messages.persist(data)


class SearchComponent:
    """
    Colleague — executes availability searches.
    FIX: corrected date-overlap SQL so results appear correctly.
    """
    def __init__(self, mediator: UIMediator):
        self.mediator = mediator

    def execute_search(self, location, start_date, end_date,
                       max_price=None, exclude_owner_id=0):
        db = get_db()

        # Base query — exclude own cars and unlisted cars
        query  = """
            SELECT c.*, u.name as owner_name
            FROM cars c
            JOIN users u ON c.owner_id = u.id
            WHERE c.is_available = 1
              AND c.owner_id != ?
        """
        params = [exclude_owner_id]

        # Location filter — only applied when user typed something.
        # Split into words so "Detroit", "MI", "Detroit MI" all match "Detroit, MI".
        if location.strip():
            location_words = location.strip().split()
            for word in location_words:
                query  += " AND LOWER(c.location) LIKE LOWER(?)"
                params.append(f"%{word}%")

        # Date filter — only applied when BOTH dates are provided.
        # Checks two things:
        #   1. The requested window falls inside the car's availability window.
        #   2. No confirmed/pending booking already overlaps the requested window.
        if start_date and end_date:
            query += """
              AND (c.avail_start IS NULL OR c.avail_start <= ?)
              AND (c.avail_end   IS NULL OR c.avail_end   >= ?)
              AND c.id NOT IN (
                  SELECT car_id FROM bookings
                  WHERE status != 'cancelled'
                    AND start_date < ?
                    AND end_date   > ?
              )
            """
            params += [start_date, end_date, end_date, start_date]

        # Price filter — only applied when a value was entered
        if max_price:
            try:
                pf = float(str(max_price).strip())
                query  += " AND CAST(c.price_per_day AS REAL) <= ?"
                params.append(pf)
            except (ValueError, AttributeError):
                pass

        results = db.execute(query, params).fetchall()
        self.mediator.notify(self, 'search_executed', results)
        return results


class BookingComponent:
    """Colleague — creates reservations and enforces no-overlap rule."""
    def __init__(self, mediator: UIMediator):
        self.mediator = mediator

    def reset(self, search_results):
        return search_results

    def is_car_available(self, car_id, start_date, end_date):
        """
        Returns True only when no active booking overlaps the requested window.
        Enforces: same car cannot be rented by >1 renter for overlapping dates.
        """
        db = get_db()
        conflict = db.execute(
            """SELECT id FROM bookings
               WHERE car_id=?
                 AND status NOT IN ('cancelled')
                 AND start_date < ?
                 AND end_date   > ?""",
            (car_id, end_date, start_date)
        ).fetchone()
        return conflict is None

    def confirm_booking(self, data):
        db = get_db()
        db.execute(
            """INSERT INTO bookings
               (car_id, renter_id, start_date, end_date, total_price, status)
               VALUES (?,?,?,?,?,'pending')""",
            (data['car_id'], data['renter_id'],
             data['start_date'], data['end_date'], data['total_price'])
        )
        db.commit()
        booking_id    = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        data['booking_id'] = booking_id
        self.mediator.notify(self, 'booking_confirmed', data)
        return booking_id


class MessageComponent:
    """Colleague — sends automated and user-composed messages."""
    def __init__(self, mediator: UIMediator):
        self.mediator = mediator

    def send_booking_confirmation(self, data):
        """Auto-message owner and renter when a booking is created."""
        db     = get_db()
        car    = db.execute("SELECT * FROM cars WHERE id=?", (data['car_id'],)).fetchone()
        renter = db.execute("SELECT name FROM users WHERE id=?", (data['renter_id'],)).fetchone()
        if not car:
            return
        renter_name = renter['name'] if renter else 'A renter'

        # To renter — booking received confirmation
        db.execute(
            "INSERT INTO messages (sender_id, receiver_id, content) VALUES (?,?,?)",
            (car['owner_id'], data['renter_id'],
             f"Hi {renter_name}! Your booking request for the "
             f"{car['make']} {car['model']} "
             f"({data['start_date']} → {data['end_date']}) "
             f"has been received. Total cost: ${data['total_price']:.2f}. "
             f"Please wait for the owner's approval.")
        )
        # To owner — new request alert
        db.execute(
            "INSERT INTO messages (sender_id, receiver_id, content) VALUES (?,?,?)",
            (data['renter_id'], car['owner_id'],
             f"New booking request from {renter_name} for your "
             f"{car['make']} {car['model']} "
             f"({data['start_date']} → {data['end_date']}). "
             f"Please approve or decline from your dashboard.")
        )
        # Notification for owner
        db.execute(
            "INSERT INTO notifications (user_id, car_id, event_type, message) VALUES (?,?,?,?)",
            (car['owner_id'], data['car_id'], 'new_booking',
             f"New booking request from {renter_name} for "
             f"{car['make']} {car['model']}.")
        )
        db.commit()

    def persist(self, data):
        """Persist a user-composed message."""
        db = get_db()
        db.execute(
            "INSERT INTO messages (sender_id, receiver_id, content) VALUES (?,?,?)",
            (data['sender_id'], data['receiver_id'], data['content'])
        )
        db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# PATTERN 4: BUILDER — CarListingBuilder
#
# Role mapping:
#   Builder interface → CarListingBuilder (abstract)
#   Concrete Builder  → ConcreteCarBuilder
#   Product           → car listing dict inserted into DB
#   Director          → route handler calls builder methods in sequence
# ─────────────────────────────────────────────────────────────────────────────
class CarListingBuilder:
    """Abstract Builder."""
    def set_basic_info(self, make, model, year): raise NotImplementedError
    def set_mileage(self, mileage):              raise NotImplementedError
    def set_location(self, location):            raise NotImplementedError
    def set_price(self, price):                  raise NotImplementedError
    def set_description(self, desc):             raise NotImplementedError
    def set_image_url(self, url):                raise NotImplementedError
    def set_availability(self, start, end):      raise NotImplementedError
    def build(self):                             raise NotImplementedError


class ConcreteCarBuilder(CarListingBuilder):
    """
    Concrete Builder — fluent interface; each setter returns self for chaining.
    build() validates required fields and returns the finished product dict.
    """
    def __init__(self, owner_id):
        self._listing = {
            'owner_id': owner_id, 'make': None, 'model': None, 'year': None,
            'mileage': 0, 'location': None, 'price_per_day': None,
            'description': '', 'image_url': '',
            'avail_start': None, 'avail_end': None, 'is_available': 1
        }

    def set_basic_info(self, make, model, year):
        self._listing.update({'make': make, 'model': model, 'year': int(year)})
        return self

    def set_mileage(self, mileage):
        self._listing['mileage'] = int(mileage) if mileage else 0
        return self

    def set_location(self, location):
        self._listing['location'] = location
        return self

    def set_price(self, price):
        self._listing['price_per_day'] = float(price)
        return self

    def set_description(self, desc):
        self._listing['description'] = desc or ''
        return self

    def set_image_url(self, url):
        self._listing['image_url'] = url or ''
        return self

    def set_availability(self, start, end):
        self._listing['avail_start'] = start or None
        self._listing['avail_end']   = end   or None
        return self

    def build(self):
        for f in ['make', 'model', 'year', 'location', 'price_per_day']:
            if not self._listing[f]:
                raise ValueError(f"Missing required field: {f}")
        return self._listing


# ─────────────────────────────────────────────────────────────────────────────
# PATTERN 5: PROXY — PaymentProxy
#
# Role mapping:
#   Subject interface → PaymentService (abstract)
#   Real Subject      → RealPaymentService (actual deduction + notification)
#   Proxy             → PaymentProxy (validation + audit log wrapper)
#
# FIX: wallet balance now correctly deducted; BOTH parties receive
#      a notification message after successful payment.
# ─────────────────────────────────────────────────────────────────────────────
class PaymentService:
    """Abstract Subject."""
    def process_payment(self, user_id, amount, booking_id):
        raise NotImplementedError


class RealPaymentService(PaymentService):
    """
    Real Subject — actual payment logic.
    Deducts from renter balance, credits owner balance,
    marks booking confirmed, notifies both parties.
    """
    def process_payment(self, user_id, amount, booking_id):
        db      = get_db()
        user    = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()

        if not booking:
            return {'success': False, 'message': 'Booking not found.'}
        if booking['status'] == 'confirmed':
            return {'success': False, 'message': 'This booking is already paid.'}
        if booking['status'] == 'cancelled':
            return {'success': False, 'message': 'This booking has been cancelled.'}
        if user['balance'] < amount:
            return {'success': False,
                    'message': (f'Insufficient balance. '
                                f'Your wallet: ${user["balance"]:.2f} | '
                                f'Required: ${amount:.2f}.')}

        car      = db.execute("SELECT * FROM cars WHERE id=?",
                              (booking['car_id'],)).fetchone()
        owner_id = car['owner_id']

        # ── Deduct from renter, credit owner ──
        db.execute("UPDATE users SET balance = balance - ? WHERE id = ?",
                   (amount, user_id))
        db.execute("UPDATE users SET balance = balance + ? WHERE id = ?",
                   (amount, owner_id))
        db.execute("UPDATE bookings SET status = 'confirmed' WHERE id = ?",
                   (booking_id,))

        # ── Notify renter ──
        db.execute(
            "INSERT INTO notifications (user_id, car_id, event_type, message) "
            "VALUES (?,?,?,?)",
            (user_id, booking['car_id'], 'payment',
             f"Payment of ${amount:.2f} confirmed for {car['make']} {car['model']} "
             f"({booking['start_date']} → {booking['end_date']}). "
             f"Enjoy your rental!")
        )
        # ── Notify owner ──
        db.execute(
            "INSERT INTO notifications (user_id, car_id, event_type, message) "
            "VALUES (?,?,?,?)",
            (owner_id, booking['car_id'], 'payment',
             f"Payment of ${amount:.2f} received for {car['make']} {car['model']} "
             f"({booking['start_date']} → {booking['end_date']}).")
        )
        # ── Message to owner ──
        db.execute(
            "INSERT INTO messages (sender_id, receiver_id, content) VALUES (?,?,?)",
            (user_id, owner_id,
             f"Hi! I've completed payment of ${amount:.2f} for your "
             f"{car['make']} {car['model']} "
             f"({booking['start_date']} → {booking['end_date']}). "
             f"Please confirm pickup details. — {user['name']}")
        )
        db.commit()
        return {'success': True,
                'message': (f'Payment of ${amount:.2f} processed! '
                            f'The owner has been notified. '
                            f'Check your messages for pickup details.')}


class PaymentProxy(PaymentService):
    """
    Proxy — wraps RealPaymentService.
    Adds: input validation, audit logging before/after, error handling.
    The route handler calls PaymentProxy, never RealPaymentService directly.
    """
    def __init__(self):
        self._real_service = RealPaymentService()   # wrapped real subject

    def process_payment(self, user_id, amount, booking_id):
        # ── Pre-check ──
        if amount <= 0:
            return {'success': False, 'message': 'Invalid payment amount.'}
        if not booking_id:
            return {'success': False, 'message': 'No booking selected.'}

        # ── Audit log: record attempt ──
        db = get_db()
        db.execute(
            "INSERT INTO payment_log (user_id, booking_id, amount, status) "
            "VALUES (?,?,?,?)",
            (user_id, booking_id, amount, 'attempted')
        )
        db.commit()

        # ── Delegate to real subject ──
        result = self._real_service.process_payment(user_id, amount, booking_id)

        # ── Audit log: update with outcome ──
        db.execute(
            "UPDATE payment_log SET status=? "
            "WHERE user_id=? AND booking_id=? AND status='attempted'",
            ('success' if result['success'] else 'failed', user_id, booking_id)
        )
        db.commit()
        return result


# ─────────────────────────────────────────────────────────────────────────────
# PATTERN 6: CHAIN OF RESPONSIBILITY — Password Recovery
#
# Role mapping:
#   Abstract Handler   → SecurityQuestionHandler
#   Concrete Handler 1 → Question1Handler
#   Concrete Handler 2 → Question2Handler
#   Concrete Handler 3 → Question3Handler
#   Client             → /recover/verify route (starts the chain at handler 1)
# ─────────────────────────────────────────────────────────────────────────────
class SecurityQuestionHandler:
    """Abstract Handler — base link with set_next() for chain assembly."""
    def __init__(self):
        self._next = None

    def set_next(self, handler):
        self._next = handler
        return handler   # allows q1.set_next(q2).set_next(q3) chaining

    def handle(self, user, answers):
        if self._next:
            return self._next.handle(user, answers)
        return {'success': True, 'message': 'Identity verified.'}


class Question1Handler(SecurityQuestionHandler):
    """Handler 1 — validates security answer 1."""
    def handle(self, user, answers):
        given = hash_password(answers.get('answer1', '').strip().lower())
        if given != user['security_answer1']:
            return {'success': False,
                    'message': 'Security question 1 answer is incorrect.'}
        return super().handle(user, answers)


class Question2Handler(SecurityQuestionHandler):
    """Handler 2 — validates security answer 2."""
    def handle(self, user, answers):
        given = hash_password(answers.get('answer2', '').strip().lower())
        if given != user['security_answer2']:
            return {'success': False,
                    'message': 'Security question 2 answer is incorrect.'}
        return super().handle(user, answers)


class Question3Handler(SecurityQuestionHandler):
    """Handler 3 — validates security answer 3 (final link in chain)."""
    def handle(self, user, answers):
        given = hash_password(answers.get('answer3', '').strip().lower())
        if given != user['security_answer3']:
            return {'success': False,
                    'message': 'Security question 3 answer is incorrect.'}
        return super().handle(user, answers)


def build_recovery_chain():
    """Assemble and return the head of the Q1 → Q2 → Q3 handler chain."""
    q1, q2, q3 = Question1Handler(), Question2Handler(), Question3Handler()
    q1.set_next(q2).set_next(q3)
    return q1


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────
def hash_password(text):
    """SHA-256 hash used for passwords and security answers."""
    return hashlib.sha256(text.encode()).hexdigest()


def login_required(f):
    """Decorator — redirects unauthenticated users to the login page."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not UserSession.get_instance().is_logged_in():
            flash('Please log in to continue.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    db   = get_db()
    user = UserSession.get_instance().get_current_user()
    # Exclude the logged-in owner's own cars — you shouldn't rent your own vehicle
    owner_id = user['id'] if user else 0
    cars = db.execute(
        "SELECT c.*, u.name as owner_name FROM cars c "
        "JOIN users u ON c.owner_id=u.id "
        "WHERE c.is_available=1 AND c.owner_id != ? "
        "ORDER BY c.id DESC LIMIT 8",
        (owner_id,)
    ).fetchall()
    return render_template('index.html', cars=cars, user=user)


# ── Authentication ─────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        db    = get_db()
        email = request.form['email'].strip().lower()
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash('Email already registered.', 'danger')
            return redirect(url_for('register'))
        db.execute(
            """INSERT INTO users
               (name,email,password,
                security_q1,security_answer1,
                security_q2,security_answer2,
                security_q3,security_answer3,balance)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (request.form['name'].strip(), email,
             hash_password(request.form['password']),
             request.form['sq1'],
             hash_password(request.form['sa1'].strip().lower()),
             request.form['sq2'],
             hash_password(request.form['sa2'].strip().lower()),
             request.form['sq3'],
             hash_password(request.form['sa3'].strip().lower()),
             500.00)
        )
        db.commit()
        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', user=None)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        db    = get_db()
        email = request.form['email'].strip().lower()
        user  = db.execute(
            "SELECT * FROM users WHERE email=? AND password=?",
            (email, hash_password(request.form['password']))
        ).fetchone()
        if user:
            UserSession.get_instance().login(dict(user))
            flash(f'Welcome back, {user["name"]}!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'danger')
    return render_template('login.html', user=None)


@app.route('/logout')
def logout():
    UserSession.get_instance().logout()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


# ── Password Recovery (Chain of Responsibility) ────────────────────────────

@app.route('/recover', methods=['GET', 'POST'])
def recover():
    if request.method == 'POST':
        db    = get_db()
        email = request.form['email'].strip().lower()
        user  = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            flash('No account found with that email.', 'danger')
            return redirect(url_for('recover'))
        session['recovery_email'] = email
        return render_template('recover_questions.html',
                               user=dict(user), nav_user=None)
    return render_template('recover.html', user=None)


@app.route('/recover/verify', methods=['POST'])
def recover_verify():
    db    = get_db()
    email = session.get('recovery_email')
    user  = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        return redirect(url_for('recover'))
    result = build_recovery_chain().handle(dict(user), request.form)
    if result['success']:
        session['can_reset_password'] = True
        return render_template('reset_password.html', user=None)
    flash(result['message'], 'danger')
    return render_template('recover_questions.html', user=dict(user), nav_user=None)


@app.route('/recover/reset', methods=['POST'])
def reset_password():
    if not session.get('can_reset_password'):
        return redirect(url_for('recover'))
    db    = get_db()
    email = session.get('recovery_email')
    db.execute("UPDATE users SET password=? WHERE email=?",
               (hash_password(request.form['new_password']), email))
    db.commit()
    session.pop('can_reset_password', None)
    session.pop('recovery_email', None)
    flash('Password reset! Please log in.', 'success')
    return redirect(url_for('login'))


# ── Dashboard ───────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    db   = get_db()
    user = UserSession.get_instance().get_current_user()
    uid  = user['id']

    my_cars = db.execute(
        "SELECT * FROM cars WHERE owner_id=?", (uid,)
    ).fetchall()

    my_bookings = db.execute(
        """SELECT b.*, c.make, c.model, c.location, c.image_url,
                  u.name as owner_name
           FROM bookings b
           JOIN cars c ON b.car_id=c.id
           JOIN users u ON c.owner_id=u.id
           WHERE b.renter_id=?
           ORDER BY b.created_at DESC""", (uid,)
    ).fetchall()

    incoming = db.execute(
        """SELECT b.*, c.make, c.model, u.name as renter_name
           FROM bookings b
           JOIN cars c ON b.car_id=c.id
           JOIN users u ON b.renter_id=u.id
           WHERE c.owner_id=? AND b.status='pending'
           ORDER BY b.created_at DESC""", (uid,)
    ).fetchall()

    notifications = db.execute(
        "SELECT * FROM notifications WHERE user_id=? "
        "ORDER BY created_at DESC LIMIT 15", (uid,)
    ).fetchall()

    unread = db.execute(
        "SELECT COUNT(*) as cnt FROM messages "
        "WHERE receiver_id=? AND is_read=0", (uid,)
    ).fetchone()['cnt']

    # Always reload balance from DB so payment deductions show immediately
    balance = db.execute(
        "SELECT balance FROM users WHERE id=?", (uid,)
    ).fetchone()['balance']

    return render_template('dashboard.html',
        user=user, my_cars=my_cars, my_bookings=my_bookings,
        incoming_bookings=incoming, notifications=notifications,
        unread_messages=unread, balance=balance)


# ── Car Listing — Builder Pattern ──────────────────────────────────────────

@app.route('/cars/new', methods=['GET', 'POST'])
@login_required
def new_car():
    user = UserSession.get_instance().get_current_user()
    if request.method == 'POST':
        try:
            listing = (
                ConcreteCarBuilder(user['id'])
                .set_basic_info(request.form['make'],
                                request.form['model'],
                                request.form['year'])
                .set_mileage(request.form.get('mileage', 0))
                .set_location(request.form['location'])
                .set_price(request.form['price_per_day'])
                .set_description(request.form.get('description', ''))
                .set_image_url(request.form.get('image_url', ''))
                .set_availability(request.form.get('avail_start'),
                                  request.form.get('avail_end'))
                .build()
            )
            db = get_db()
            db.execute(
                """INSERT INTO cars
                   (owner_id,make,model,year,mileage,location,price_per_day,
                    description,image_url,avail_start,avail_end,is_available)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
                (listing['owner_id'], listing['make'], listing['model'],
                 listing['year'], listing['mileage'], listing['location'],
                 listing['price_per_day'], listing['description'],
                 listing['image_url'], listing['avail_start'], listing['avail_end'])
            )
            db.commit()
            flash('Car listed successfully!', 'success')
            return redirect(url_for('dashboard'))
        except ValueError as e:
            flash(str(e), 'danger')
    return render_template('new_car.html', user=user)


@app.route('/cars/<int:car_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_car(car_id):
    """
    Owner edits listing.
    FIX: image_url now editable; is_available=0 properly delists the car.
    Observer fires on price drop OR re-listing.
    """
    db   = get_db()
    car  = db.execute("SELECT * FROM cars WHERE id=?", (car_id,)).fetchone()
    user = UserSession.get_instance().get_current_user()

    if not car or car['owner_id'] != user['id']:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        new_price  = float(request.form['price_per_day'])
        old_price  = float(car['price_per_day'])
        new_status = int(request.form.get('is_available', 1))
        old_status = int(car['is_available'])

        db.execute(
            """UPDATE cars
               SET price_per_day=?, avail_start=?, avail_end=?,
                   is_available=?, description=?, image_url=?
               WHERE id=?""",
            (new_price,
             request.form.get('avail_start') or None,
             request.form.get('avail_end') or None,
             new_status,
             request.form.get('description', ''),
             request.form.get('image_url', ''),
             car_id)
        )
        db.commit()

        # Observer: price drop notification
        if new_price < old_price:
            notify_watchers(car_id, 'price_drop',
                f"{car['make']} {car['model']} price dropped "
                f"to ${new_price:.0f}/day!")

        # Observer: car re-listed notification
        if new_status == 1 and old_status == 0:
            notify_watchers(car_id, 'available',
                f"{car['make']} {car['model']} is now available for rental!")

        flash('Listing updated!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('edit_car.html', car=car, user=user)


@app.route('/cars/<int:car_id>')
def car_detail(car_id):
    db  = get_db()
    car = db.execute(
        "SELECT c.*, u.name as owner_name, u.id as owner_id "
        "FROM cars c JOIN users u ON c.owner_id=u.id WHERE c.id=?", (car_id,)
    ).fetchone()
    if not car:
        flash('Car not found.', 'danger')
        return redirect(url_for('index'))

    reviews = db.execute(
        "SELECT r.*, u.name as reviewer_name FROM reviews r "
        "JOIN users u ON r.reviewer_id=u.id "
        "WHERE r.car_id=? ORDER BY r.created_at DESC", (car_id,)
    ).fetchall()

    avg_rating = db.execute(
        "SELECT AVG(rating) as avg FROM reviews WHERE car_id=?", (car_id,)
    ).fetchone()['avg']

    user        = UserSession.get_instance().get_current_user()
    is_watching = False
    if user:
        is_watching = bool(db.execute(
            "SELECT 1 FROM watchlist WHERE car_id=? AND renter_id=?",
            (car_id, user['id'])
        ).fetchone())

    # Preserve dates if redirected back after a validation error
    start_date = request.args.get('start_date', '')
    end_date   = request.args.get('end_date', '')

    return render_template('car_detail.html',
        car=car, reviews=reviews, avg_rating=avg_rating,
        user=user, is_watching=is_watching,
        start_date=start_date, end_date=end_date)


# ── Search ──────────────────────────────────────────────────────────────────

@app.route('/search')
def search():
    location   = request.args.get('location', '').strip()
    start_date = request.args.get('start_date', '').strip()
    end_date   = request.args.get('end_date', '').strip()
    max_price  = request.args.get('max_price', '').strip() or None
    show_all   = request.args.get('show_all', '0')   # "1" = browse all listings
    user       = UserSession.get_instance().get_current_user()

    results = None
    # Run a search whenever the user submitted the form (any param present)
    # OR clicked Show All. This way Search with no filters = show everything.
    searched = any([location, start_date, end_date, max_price, show_all == '1'])
    if searched:
        mediator = DriveShareMediator()
        owner_id = user['id'] if user else 0
        results  = mediator.search.execute_search(
            location, start_date, end_date, max_price,
            exclude_owner_id=owner_id)

    return render_template('search.html', cars=results, user=user,
                           location=location, start_date=start_date,
                           end_date=end_date, max_price=max_price or '',
                           show_all=show_all)


# ── Booking ─────────────────────────────────────────────────────────────────

@app.route('/book/<int:car_id>', methods=['POST'])
@login_required
def book_car(car_id):
    """
    Create a booking via the Mediator's BookingComponent.
    FIX: overlap check prevents double-booking the same car.
    """
    user = UserSession.get_instance().get_current_user()
    db   = get_db()
    car  = db.execute("SELECT * FROM cars WHERE id=?", (car_id,)).fetchone()

    if not car:
        flash('Car not found.', 'danger')
        return redirect(url_for('index'))
    if car['owner_id'] == user['id']:
        flash('You cannot rent your own car.', 'danger')
        return redirect(url_for('car_detail', car_id=car_id))

    start_date = request.form['start_date']
    end_date   = request.form['end_date']

    try:
        d1   = datetime.strptime(start_date, '%Y-%m-%d')
        d2   = datetime.strptime(end_date,   '%Y-%m-%d')
        days = (d2 - d1).days
    except ValueError:
        flash('Invalid date format.', 'danger')
        return redirect(url_for('car_detail', car_id=car_id,
                                start_date=start_date, end_date=end_date))

    if days <= 0:
        flash('Return date must be after pick-up date.', 'danger')
        return redirect(url_for('car_detail', car_id=car_id,
                                start_date=start_date, end_date=end_date))

    mediator = DriveShareMediator()
    if not mediator.booking.is_car_available(car_id, start_date, end_date):
        flash('This car is already booked for those dates. '
              'Please choose different dates.', 'danger')
        return redirect(url_for('car_detail', car_id=car_id))

    total_price = days * car['price_per_day']
    mediator.booking.confirm_booking({
        'car_id': car_id, 'renter_id': user['id'],
        'start_date': start_date, 'end_date': end_date,
        'total_price': total_price
    })

    flash(f'Booking request sent for {days} day(s) — '
          f'Total: ${total_price:.2f}. Waiting for owner approval.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/booking/<int:booking_id>/cancel', methods=['POST'])
@login_required
def cancel_booking(booking_id):
    """Renter cancels a pending or approved (unpaid) booking."""
    db      = get_db()
    user    = UserSession.get_instance().get_current_user()
    booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()

    if not booking or booking['renter_id'] != user['id']:
        flash('Booking not found.', 'danger')
        return redirect(url_for('dashboard'))
    if booking['status'] == 'confirmed':
        flash('Cannot cancel a booking that has already been paid.', 'danger')
        return redirect(url_for('dashboard'))

    db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
    car = db.execute("SELECT * FROM cars WHERE id=?", (booking['car_id'],)).fetchone()
    db.execute(
        "INSERT INTO notifications (user_id, car_id, event_type, message) VALUES (?,?,?,?)",
        (car['owner_id'], car['id'], 'cancelled',
         f"{user['name']} cancelled their booking for "
         f"{car['make']} {car['model']} "
         f"({booking['start_date']} → {booking['end_date']}).")
    )
    db.commit()
    flash('Booking cancelled.', 'info')
    return redirect(url_for('dashboard'))


# ── Watchlist (Observer) ────────────────────────────────────────────────────

@app.route('/watch/<int:car_id>', methods=['POST'])
@login_required
def toggle_watch(car_id):
    user = UserSession.get_instance().get_current_user()
    db   = get_db()
    existing = db.execute(
        "SELECT 1 FROM watchlist WHERE car_id=? AND renter_id=?",
        (car_id, user['id'])
    ).fetchone()

    if existing:
        db.execute("DELETE FROM watchlist WHERE car_id=? AND renter_id=?",
                   (car_id, user['id']))
        flash('Removed from watchlist.', 'info')
    else:
        mp = request.form.get('max_price') or None
        db.execute(
            "INSERT INTO watchlist (car_id, renter_id, max_price) VALUES (?,?,?)",
            (car_id, user['id'], mp)
        )
        flash('Watching this car! You will be notified of price drops '
              'and availability changes.', 'success')
    db.commit()
    return redirect(url_for('car_detail', car_id=car_id))


# ── Payment (Proxy) ─────────────────────────────────────────────────────────

@app.route('/pay/<int:booking_id>', methods=['POST'])
@login_required
def pay(booking_id):
    """Pay via PaymentProxy — validates, logs, deducts, notifies both parties."""
    user    = UserSession.get_instance().get_current_user()
    db      = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()

    if not booking or booking['renter_id'] != user['id']:
        flash('Invalid booking.', 'danger')
        return redirect(url_for('dashboard'))

    result = PaymentProxy().process_payment(
        user['id'], booking['total_price'], booking_id)
    flash(result['message'], 'success' if result['success'] else 'danger')
    return redirect(url_for('dashboard'))


# ── Approve booking (owner) ────────────────────────────────────────────────

@app.route('/booking/<int:booking_id>/approve', methods=['POST'])
@login_required
def approve_booking(booking_id):
    db      = get_db()
    user    = UserSession.get_instance().get_current_user()
    booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    car     = db.execute("SELECT * FROM cars WHERE id=?", (booking['car_id'],)).fetchone()

    if car['owner_id'] != user['id']:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('dashboard'))

    db.execute("UPDATE bookings SET status='approved' WHERE id=?", (booking_id,))
    db.execute(
        "INSERT INTO notifications (user_id, car_id, event_type, message) VALUES (?,?,?,?)",
        (booking['renter_id'], car['id'], 'approved',
         f"Your booking for {car['make']} {car['model']} was approved! "
         f"Please complete payment from your dashboard.")
    )
    db.execute(
        "INSERT INTO messages (sender_id, receiver_id, content) VALUES (?,?,?)",
        (user['id'], booking['renter_id'],
         f"Great news! I've approved your booking for the "
         f"{car['make']} {car['model']} "
         f"({booking['start_date']} → {booking['end_date']}). "
         f"Please complete payment of ${booking['total_price']:.2f}. "
         f"— {user['name']}")
    )
    db.commit()
    flash('Booking approved! Renter notified to complete payment.', 'success')
    return redirect(url_for('dashboard'))


# ── Messaging ────────────────────────────────────────────────────────────────

@app.route('/messages')
@login_required
def messages():
    """
    Inbox — shows all sent and received messages.
    FIX: now shows conversations both ways; no raw ID required to compose.
    """
    user = UserSession.get_instance().get_current_user()
    db   = get_db()

    msgs = db.execute(
        """SELECT m.*,
                  su.name as sender_name,
                  ru.name as receiver_name
           FROM messages m
           JOIN users su ON m.sender_id   = su.id
           JOIN users ru ON m.receiver_id = ru.id
           WHERE m.receiver_id=? OR m.sender_id=?
           ORDER BY m.created_at DESC""",
        (user['id'], user['id'])
    ).fetchall()

    db.execute("UPDATE messages SET is_read=1 WHERE receiver_id=?", (user['id'],))
    db.commit()

    other_users = db.execute(
        "SELECT id, name FROM users WHERE id != ?", (user['id'],)
    ).fetchall()

    return render_template('messages.html',
                           messages=msgs, user=user, other_users=other_users)


@app.route('/messages/send', methods=['POST'])
@login_required
def send_message():
    """
    Send a message via the Mediator.
    FIX: validates receiver exists before insert (prevents IntegrityError).
    """
    user = UserSession.get_instance().get_current_user()
    db   = get_db()

    try:
        receiver_id = int(request.form.get('receiver_id', 0))
    except (ValueError, TypeError):
        flash('Invalid recipient.', 'danger')
        return redirect(url_for('messages'))

    receiver = db.execute(
        "SELECT id, name FROM users WHERE id=?", (receiver_id,)
    ).fetchone()
    if not receiver:
        flash('Recipient not found.', 'danger')
        return redirect(url_for('messages'))

    content = request.form.get('content', '').strip()
    if not content:
        flash('Message cannot be empty.', 'danger')
        return redirect(url_for('messages'))

    DriveShareMediator().notify(None, 'message_sent', {
        'sender_id':   user['id'],
        'receiver_id': receiver_id,
        'content':     content
    })
    flash(f'Message sent to {receiver["name"]}!', 'success')
    return redirect(url_for('messages'))


# ── Reviews ─────────────────────────────────────────────────────────────────

@app.route('/review/<int:car_id>', methods=['POST'])
@login_required
def add_review(car_id):
    user = UserSession.get_instance().get_current_user()
    db   = get_db()
    booking = db.execute(
        "SELECT * FROM bookings WHERE car_id=? AND renter_id=? AND status='confirmed'",
        (car_id, user['id'])
    ).fetchone()
    if not booking:
        flash('You can only review cars you have a confirmed (paid) rental for.', 'danger')
        return redirect(url_for('car_detail', car_id=car_id))
    db.execute(
        "INSERT INTO reviews (car_id, reviewer_id, rating, comment) VALUES (?,?,?,?)",
        (car_id, user['id'],
         int(request.form['rating']),
         request.form.get('comment', '').strip())
    )
    db.commit()
    flash('Review submitted!', 'success')
    return redirect(url_for('car_detail', car_id=car_id))


# ── Rental History ───────────────────────────────────────────────────────────

@app.route('/history')
@login_required
def rental_history():
    user = UserSession.get_instance().get_current_user()
    db   = get_db()

    as_renter = db.execute(
        """SELECT b.*, c.make, c.model, c.location, c.image_url,
                  u.name as owner_name
           FROM bookings b
           JOIN cars c ON b.car_id=c.id
           JOIN users u ON c.owner_id=u.id
           WHERE b.renter_id=? ORDER BY b.created_at DESC""",
        (user['id'],)
    ).fetchall()

    as_owner = db.execute(
        """SELECT b.*, c.make, c.model, u.name as renter_name
           FROM bookings b
           JOIN cars c ON b.car_id=c.id
           JOIN users u ON b.renter_id=u.id
           WHERE c.owner_id=? ORDER BY b.created_at DESC""",
        (user['id'],)
    ).fetchall()

    return render_template('history.html',
                           as_renter=as_renter, as_owner=as_owner, user=user)


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    app.run(debug=True)
