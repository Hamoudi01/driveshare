# DriveShare — CIS 476 Term Project
**Peer-to-Peer Car Rental Platform**  
Built with Python (Flask) + SQLite + HTML/CSS

---

## Quick Setup (Do This Once)

### 1. Install Python
Download from https://python.org — choose the latest version.  
During install, **check the box "Add Python to PATH"**.

### 2. Install VS Code
Download from https://code.visualstudio.com — free editor.

### 3. Open the project
- Unzip the project folder
- Open VS Code → File → Open Folder → select `driveshare/`

### 4. Open the built-in terminal in VS Code
Press: `` Ctrl + ` `` (backtick key, top-left of keyboard)

### 5. Install Flask (one command)
```
pip install flask
```

### 6. Run the app
```

cd ~/OneDrive/Documents/driveshare_project/driveshare

python app.py
```

### 7. Open your browser
Go to: **http://127.0.0.1:5000**

That's it! The database creates itself automatically on first run.

---

### 8. Terminal used

I used git bash termianl also known as MINGW64.

## Demo Accounts (Pre-loaded)

| Name         | Email            | Password    | Role          |
|--------------|------------------|-------------|---------------|
| Alice Owner  | alice@demo.com   | password123 | Owner         |
| Bob Renter   | bob@demo.com     | password123 | Renter        |
| Carol Driver | carol@demo.com   | password123 | Owner/Renter  |

Security question answers (all lowercase):
- Alice: fluffy / chicago / smith
- Bob: rex / detroit / jones
- Carol: whiskers / miami / brown

---

## Project Structure

```
driveshare/
│
├── app.py              ← Main application + ALL 6 design patterns
├── database.py         ← Database schema, init, seed data
├── requirements.txt    ← Flask dependency
│
├── templates/          ← HTML pages (Jinja2 templates)
│   ├── base.html           Base layout + navigation
│   ├── index.html          Home page
│   ├── register.html       User registration
│   ├── login.html          Login page
│   ├── recover.html        Password recovery step 1
│   ├── recover_questions.html  Security questions (Chain of Responsibility)
│   ├── reset_password.html Password reset
│   ├── dashboard.html      User dashboard
│   ├── search.html         Car search results
│   ├── car_detail.html     Individual car page + booking
│   ├── new_car.html        List a new car (Builder pattern)
│   ├── edit_car.html       Edit listing (Observer pattern fires here)
│   ├── messages.html       Messaging inbox
│   └── history.html        Rental history
│
└── instance/
    └── driveshare.db   ← SQLite database (auto-created on first run)
```

---

## Design Patterns Implemented

| Pattern                  | Location in app.py         | Purpose                                        |
|--------------------------|----------------------------|------------------------------------------------|
| **Singleton**            | `UserSession`              | One session manager for all auth operations    |
| **Observer**             | `CarListingSubject` / `WatchlistObserver` | Notify renters of price drops / bookings |
| **Mediator**             | `DriveShareMediator`       | Coordinate Search, Booking, Message components |
| **Builder**              | `ConcreteCarBuilder`       | Construct car listings step-by-step            |
| **Proxy**                | `PaymentProxy`             | Validate, log, and delegate payment calls      |
| **Chain of Responsibility** | `Question1/2/3Handler` | 3-step password recovery verification         |

---

## Database Tables

| Table          | Purpose                                      |
|----------------|----------------------------------------------|
| users          | Registered users, hashed passwords + answers |
| cars           | Vehicle listings by owners                   |
| bookings       | Rental reservations                          |
| watchlist      | Observer subscriptions (renter → car)        |
| notifications  | Observer-generated alerts                    |
| messages       | In-app messaging                             |
| reviews        | Post-rental ratings + comments               |
| payment_log    | Proxy audit trail for payments               |

---

## Stopping the App
Press `Ctrl + C` in the terminal.
