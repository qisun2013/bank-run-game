# CLAUDE.md

## Project Overview

**Bank Run Game** is an educational web application that simulates bank run dynamics for classroom use. It teaches game theory and macroeconomic concepts (multiple Nash equilibria, herding behavior, self-fulfilling prophecies, monetary policy interventions) through an interactive multi-round simulation where students choose to withdraw or stay.

## Tech Stack

- **Backend:** Python 3 with Flask 3.0.3
- **Frontend:** Server-rendered Jinja2 templates with inline HTML/CSS/JavaScript (vanilla JS, no framework)
- **Production Server:** Gunicorn 22.0.0
- **Deployment:** Render.com (configured in `render.yaml`)
- **Database:** None — all state is held in-memory in a global `STATE` dictionary

## Project Structure

```
bank-run-game/
├── app.py              # Entire application (752 lines, monolithic)
├── requirements.txt    # Python dependencies (flask, gunicorn)
├── render.yaml         # Render.com deployment config
├── README.md           # Minimal project description
└── CLAUDE.md           # This file
```

This is a single-file application. All code — models, game logic, HTML templates, CSS, routes — lives in `app.py`.

## Architecture of app.py

The file is organized into these sections (in order):

| Lines     | Section                | Description                                          |
|-----------|------------------------|------------------------------------------------------|
| 1–12      | Imports & Config       | Flask setup, `TEACHER_KEY`, `PORT`                   |
| 15–57     | Data Models            | `Params` and `Player` dataclasses                    |
| 60–67     | Global State           | `STATE` dict (params, players, phase, round, history)|
| 70–282    | Game Logic Functions   | Round management, settlement, balance calculations   |
| 287–574   | UI Templates           | CSS string + Jinja2 HTML templates (inline strings)  |
| 577–752   | Route Handlers         | Flask routes for student and teacher interfaces      |

## Key Concepts

### Game Phases

`STATE["phase"]` transitions through: `lobby` → `collect` → `reveal` → (repeat or `finished`)

- **lobby**: Students join, teacher configures parameters
- **collect**: Students choose Withdraw (W) or Stay (S) within a time limit
- **reveal**: Round outcomes are calculated and displayed
- **finished**: All rounds completed

### Settlement Modes

- **Queue mode** (`queue_mode=True`): First-come-first-served — early withdrawers get paid in full first, later ones may get partial or nothing
- **Pro-rata mode** (`queue_mode=False`): Available cash is shared proportionally among all withdrawers

### Policy Interventions (teacher-configurable)

- **Deposit insurance**: Caps payouts at `insurance_cap`
- **Lender of Last Resort (LoLR)**: Injects emergency cash (`lolr_limit`) into the bank
- **Bad news signal**: Reduces perceived long-asset return by `news_severity`

### State Management

All game state lives in a single global `STATE` dict. There is no database or persistence — state is lost on server restart. The application must run with a single Gunicorn worker (`--workers 1`) because of this shared in-memory state.

## Routes

### Student Routes (public)

| Route         | Method | Purpose                                    |
|---------------|--------|--------------------------------------------|
| `/`           | GET    | Join/login page                            |
| `/join`       | POST   | Create player, redirect to game page       |
| `/s/<pid>`    | GET    | Main game page (adapts to current phase)   |
| `/choose`     | GET    | Record W/S choice (`?pid=...&c=W`)         |
| `/signal`     | GET    | JSON endpoint for live withdrawal count    |

### Teacher Routes (require `?key=TEACHER_KEY`)

| Route              | Method | Purpose                              |
|--------------------|--------|--------------------------------------|
| `/teacher`         | GET    | Dashboard with params, players, history |
| `/teacher/params`  | POST   | Update game parameters               |
| `/teacher/action`  | GET    | Actions: start, reveal, lock, reset  |

## Development

### Local Setup

```bash
pip install -r requirements.txt
python app.py
```

The app runs on `http://localhost:8000` by default.

### Access the Teacher Dashboard

Navigate to `/teacher?key=CHANGE_ME_TO_SOMETHING_SECRET` (the default key in development).

### Production Deployment

Deployed to Render.com using `render.yaml`:
```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1
```

The single-worker constraint is required — multiple workers would create separate in-memory states.

### No Tests or Linting

There is currently no test suite, no linting configuration, and no CI/CD pipeline.

## Important Conventions and Constraints

- **Single-file architecture**: All changes go in `app.py`. Do not split into modules unless explicitly requested.
- **Inline templates**: HTML templates are Python string variables (`JOIN_PAGE`, `STUDENT_PAGE`, `TEACHER_PAGE`) using Jinja2 syntax. There is no `templates/` directory.
- **Inline CSS**: Styles are in the `CSS` string variable, shared across all pages.
- **No external JS**: Frontend uses vanilla JavaScript with AJAX polling (800ms interval to `/signal`).
- **In-memory state only**: No database. State resets on restart. Do not introduce persistence unless asked.
- **Single worker**: The app assumes a single process. Do not add async workers or multi-process patterns.
- **Chinese comments**: Some comments are in Chinese (this is an educational tool for Chinese-speaking students). Preserve bilingual comments when editing.
- **`TEACHER_KEY` is hardcoded**: The default value `"CHANGE_ME_TO_SOMETHING_SECRET"` is for development. In production it should be set differently. Do not commit real secrets.
- **Player auth is URL-based**: Players are identified by `pid` tokens in URLs, not sessions/cookies.

## Common Modification Patterns

### Adding a new game parameter

1. Add the field to the `Params` dataclass (line ~18)
2. Add a form input for it in `TEACHER_PAGE` template
3. Parse it in the `teacher_params()` route handler (line ~680)
4. Use it in the game logic functions as needed

### Changing settlement logic

Modify `reveal_round()` (line ~135). This is the core function that calculates payouts for each round.

### Modifying the student UI

Edit the `STUDENT_PAGE` string variable (line ~287+). It's a Jinja2 template receiving context variables from the `student()` route.

### Adding a new route

Add a new `@app.route()` decorated function in the routes section (after line ~577). Follow existing patterns for teacher auth (`require_teacher()`) and auto-reveal (`maybe_auto_reveal()`).
