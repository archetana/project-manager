#!/usr/bin/env python3
"""
update_board.py — NemoClaw Autonomous PM Agent Script
Enhanced with: task extraction, velocity tracking, predictive milestone warnings, execution analytics.

Modes:
  update       — Process a team message, update JSON data + dashboard
  digest       — Generate daily summary
  escalate     — Check for stale blockers and milestone risks
  report       — Generate team or leadership reports
  task-extract — Extract implicit tasks from a message
  analytics    — Generate execution analytics report
  velocity     — Update velocity tracking data

Usage:
  python3 update_board.py update "<message>" "<author>" "<classification>"
  python3 update_board.py digest
  python3 update_board.py escalate
  python3 update_board.py report team|leadership
  python3 update_board.py task-extract "<message>" "<author>"
  python3 update_board.py analytics
  python3 update_board.py velocity
"""

import json
import os
import sys
import base64
import subprocess
from datetime import datetime, timedelta
from difflib import SequenceMatcher

# ============================================================
# CONFIG
# ============================================================
GITHUB_REPO = "archetana/project-manager"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents"
ENV_FILE = "/sandbox/.env" if os.path.exists("/sandbox/.env") else os.path.join(os.path.dirname(__file__), ".env")
PROXY = os.environ.get("HTTP_PROXY", os.environ.get("http_proxy", ""))

def get_token():
    """Load GitHub token from .env file."""
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith("GITHUB_TOKEN="):
                    return line.strip().split("=", 1)[1]
    return os.environ.get("GITHUB_TOKEN", "")

TOKEN = get_token()

# ============================================================
# GITHUB API HELPERS
# ============================================================

def github_get(filepath):
    """GET a file from GitHub repo. Returns (content_dict, sha)."""
    url = f"{GITHUB_API}/{filepath}"
    cmd = ["curl", "-s", "-H", f"Authorization: token {TOKEN}",
           "-H", "Accept: application/vnd.github.v3+json", url]
    if PROXY:
        cmd = ["curl", "-s", "--proxy", PROXY] + cmd[1:]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    content = base64.b64decode(data.get("content", "")).decode("utf-8")
    return json.loads(content) if filepath.endswith(".json") else content, data.get("sha", "")

def github_put(filepath, content, sha, message="Auto-update via NemoClaw"):
    """PUT (update) a file on GitHub."""
    if isinstance(content, (dict, list)):
        content = json.dumps(content, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    payload = json.dumps({"message": message, "content": encoded, "sha": sha})
    url = f"{GITHUB_API}/{filepath}"
    cmd = ["curl", "-s", "-X", "PUT",
           "-H", f"Authorization: token {TOKEN}",
           "-H", "Accept: application/vnd.github.v3+json",
           "-d", payload, url]
    if PROXY:
        cmd = ["curl", "-s", "--proxy", PROXY, "-X", "PUT"] + cmd[3:]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(result.stdout)

# ============================================================
# MODE: UPDATE
# ============================================================

def mode_update(message, author, classification):
    """Process a team status update."""
    # Load current data
    updates, updates_sha = github_get("updates.json")
    
    # Create new entry
    entry_id = f"u{len(updates)+1:04d}"
    status_map = {
        "blocker": "blocked",
        "blocked": "blocked",
        "delayed": "delayed",
        "progress": "ontrack",
        "completed": "completed",
        "task": "ontrack",
        "risk": "delayed",
        "ontrack": "ontrack"
    }
    
    new_entry = {
        "id": entry_id,
        "author": author,
        "message": message,
        "timestamp": datetime.now().isoformat(),
        "status": status_map.get(classification, "ontrack"),
        "classification": classification,
        "resolved": False
    }
    
    updates.append(new_entry)
    
    # Save updates
    github_put("updates.json", updates, updates_sha, f"Update from {author}: {classification}")
    
    # Auto-extract task
    task_result = mode_task_extract(message, author, classification)
    
    # Update velocity
    mode_velocity_update()
    
    # Check if blocker → create GitHub issue
    issue_num = None
    if classification in ("blocker", "blocked"):
        issue_num = create_github_issue(message, author)
    
    # Check for escalation
    escalation = check_escalation_needed(updates)
    
    output = f"SUCCESS | Entry {entry_id} added | Status: {classification}"
    if task_result:
        output += f" | Task: {task_result}"
    if issue_num:
        output += f" | Issue #{issue_num} created"
    if escalation:
        output += f" | ⚠️ ESCALATION: {escalation}"
    
    print(output)
    return output

# ============================================================
# MODE: TASK-EXTRACT
# ============================================================

def mode_task_extract(message, author, classification=None):
    """Extract implicit tasks from a message and track them."""
    tasks, tasks_sha = github_get("tasks.json")
    
    # Check if this message matches an existing task (fuzzy)
    matched_task = None
    for task in tasks:
        similarity = SequenceMatcher(None, message.lower(), task["title"].lower()).ratio()
        if similarity > 0.4:
            matched_task = task
            break
    
    if matched_task:
        # Update existing task
        status_map = {"blocker": "blocked", "blocked": "blocked", "delayed": "delayed", 
                      "completed": "completed", "progress": "in-progress", "ontrack": "in-progress"}
        matched_task["status"] = status_map.get(classification, matched_task["status"])
        matched_task["updated_at"] = datetime.now().isoformat()
        github_put("tasks.json", tasks, tasks_sha, f"Task updated: {matched_task['id']}")
        return f"Updated existing task {matched_task['id']}"
    else:
        # Create new task
        task_id = f"t{len(tasks)+1:03d}"
        new_task = {
            "id": task_id,
            "title": message[:80],
            "owner": author,
            "status": "in-progress" if classification in ("task", "progress", "ontrack") else classification or "in-progress",
            "created_from": None,
            "github_issue": None,
            "stream": "Unassigned",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }
        tasks.append(new_task)
        github_put("tasks.json", tasks, tasks_sha, f"New task extracted: {task_id}")
        return f"New task {task_id} created"

# ============================================================
# MODE: VELOCITY
# ============================================================

def mode_velocity_update():
    """Update daily velocity tracking data."""
    velocity, vel_sha = github_get("velocity.json")
    updates, _ = github_get("updates.json")
    tasks, _ = github_get("tasks.json")
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Count today's metrics
    today_completed = len([t for t in tasks if t.get("status") == "completed" and 
                          t.get("updated_at", "").startswith(today)])
    active_blockers = len([u for u in updates if u["classification"] == "blocker" and not u["resolved"]])
    
    # Calculate progress from tasks
    total_tasks = len(tasks)
    completed_tasks = len([t for t in tasks if t.get("status") == "completed"])
    progress_pct = round((completed_tasks / total_tasks * 100)) if total_tasks > 0 else 0
    
    # Update or append today's entry
    today_entry = None
    for entry in velocity:
        if entry["date"] == today:
            today_entry = entry
            break
    
    if today_entry:
        today_entry["tasks_completed"] = max(today_entry["tasks_completed"], today_completed)
        today_entry["blockers_active"] = active_blockers
        today_entry["progress_pct"] = progress_pct
    else:
        velocity.append({
            "date": today,
            "tasks_completed": today_completed,
            "blockers_active": active_blockers,
            "progress_pct": progress_pct
        })
    
    # Keep only last 30 days
    velocity = velocity[-30:]
    
    github_put("velocity.json", velocity, vel_sha, f"Velocity update: {today}")
    print(f"Velocity updated for {today}: {today_completed} tasks, {active_blockers} blockers, {progress_pct}% progress")

# ============================================================
# MODE: ESCALATE
# ============================================================

def mode_escalate():
    """Check for stale blockers and milestone risks."""
    updates, _ = github_get("updates.json")
    milestones, _ = github_get("milestones.json")
    
    now = datetime.now()
    alerts = []
    
    # Check stale blockers (>48h)
    for u in updates:
        if u["classification"] == "blocker" and not u["resolved"]:
            created = datetime.fromisoformat(u["timestamp"])
            hours_old = (now - created).total_seconds() / 3600
            if hours_old > 48:
                alerts.append(f"🔴 STALE BLOCKER ({int(hours_old)}h): {u['author']} — {u['message']}")
    
    # Check milestone risks
    active_blockers = [u for u in updates if u["classification"] == "blocker" and not u["resolved"]]
    for m in milestones:
        target = datetime.strptime(m["date"], "%Y-%m-%d")
        days_left = (target - now).days
        if days_left <= 3 and active_blockers:
            alerts.append(f"🚨 CRITICAL: '{m['name']}' in {days_left} day(s) with {len(active_blockers)} active blocker(s)")
        elif days_left <= 7 and active_blockers:
            alerts.append(f"⚠️ WARNING: '{m['name']}' in {days_left} days, {len(active_blockers)} blocker(s) unresolved")
    
    # Velocity-based prediction
    velocity, _ = github_get("velocity.json")
    if len(velocity) >= 3:
        avg_velocity = sum(v["tasks_completed"] for v in velocity[-7:]) / min(len(velocity), 7)
        if avg_velocity < 1.5:
            alerts.append(f"📉 LOW VELOCITY: Avg {avg_velocity:.1f} tasks/day (below 1.5 threshold)")
    
    if alerts:
        print("ESCALATION REPORT:")
        for alert in alerts:
            print(f"  {alert}")
    else:
        print("✅ All clear — no escalations needed.")
    
    return alerts

def check_escalation_needed(updates):
    """Quick check if escalation is needed (called inline during updates)."""
    now = datetime.now()
    for u in updates:
        if u["classification"] == "blocker" and not u["resolved"]:
            created = datetime.fromisoformat(u["timestamp"])
            if (now - created).total_seconds() / 3600 > 48:
                return f"Blocker from {u['author']} is stale ({int((now - created).total_seconds() / 3600)}h)"
    return None

# ============================================================
# MODE: DIGEST
# ============================================================

def mode_digest():
    """Generate daily summary."""
    updates, _ = github_get("updates.json")
    milestones, _ = github_get("milestones.json")
    velocity, _ = github_get("velocity.json")
    
    now = datetime.now()
    yesterday = now - timedelta(hours=24)
    
    # Filter last 24h
    recent = [u for u in updates if datetime.fromisoformat(u["timestamp"]) > yesterday]
    
    # Group by status
    by_status = {}
    for u in recent:
        status = u["classification"]
        by_status.setdefault(status, []).append(u)
    
    # Active blockers (all time, unresolved)
    active_blockers = [u for u in updates if u["classification"] == "blocker" and not u["resolved"]]
    
    # Velocity
    avg_vel = 0
    if velocity:
        avg_vel = sum(v["tasks_completed"] for v in velocity[-7:]) / min(len(velocity), 7)
    
    print("=" * 50)
    print(f"📋 DAILY DIGEST — {now.strftime('%B %d, %Y')}")
    print("=" * 50)
    print(f"\n📊 Activity: {len(recent)} updates in last 24h")
    print(f"📈 Velocity: {avg_vel:.1f} tasks/day (7-day avg)")
    
    if by_status.get("progress"):
        print(f"\n✅ Progress ({len(by_status['progress'])}):")
        for u in by_status["progress"]:
            print(f"   • {u['author']}: {u['message']}")
    
    if active_blockers:
        print(f"\n🚫 Active Blockers ({len(active_blockers)}):")
        for u in active_blockers:
            age = (now - datetime.fromisoformat(u["timestamp"])).total_seconds() / 3600
            print(f"   • {u['author']}: {u['message']} ({int(age)}h old)")
    
    if by_status.get("delayed"):
        print(f"\n⚠️ Delays ({len(by_status['delayed'])}):")
        for u in by_status["delayed"]:
            print(f"   • {u['author']}: {u['message']}")
    
    # Milestone warnings
    print(f"\n🎯 Upcoming Milestones:")
    for m in milestones:
        target = datetime.strptime(m["date"], "%Y-%m-%d")
        days_left = (target - now).days
        risk = "🔴 AT RISK" if (active_blockers and days_left <= 3) else "⚠️ WATCH" if (active_blockers and days_left <= 7) else "✅"
        print(f"   {risk} {m['name']} — {days_left} days ({m['date']})")
    
    print("\n" + "=" * 50)

# ============================================================
# MODE: REPORT
# ============================================================

def mode_report(report_type):
    """Generate team or leadership report."""
    updates, _ = github_get("updates.json")
    milestones, _ = github_get("milestones.json")
    velocity, _ = github_get("velocity.json")
    tasks, _ = github_get("tasks.json")
    
    now = datetime.now()
    
    if report_type == "team":
        print("=" * 50)
        print(f"👥 TEAM ACTIVITY REPORT — {now.strftime('%B %d, %Y')}")
        print("=" * 50)
        
        # Group by author
        by_author = {}
        for u in updates:
            by_author.setdefault(u["author"], []).append(u)
        
        for author, entries in sorted(by_author.items()):
            recent = [e for e in entries if datetime.fromisoformat(e["timestamp"]) > now - timedelta(days=7)]
            last_update = max(entries, key=lambda e: e["timestamp"])
            hours_since = (now - datetime.fromisoformat(last_update["timestamp"])).total_seconds() / 3600
            
            status_icon = "🔴" if hours_since > 48 else "🟡" if hours_since > 24 else "🟢"
            print(f"\n{status_icon} {author}")
            print(f"   Last active: {int(hours_since)}h ago")
            print(f"   Updates (7d): {len(recent)}")
            if recent:
                print(f"   Latest: {recent[-1]['message'][:60]}")
        
        # Silent members warning
        all_authors = set(by_author.keys())
        expected = {"Pratik Karanjule", "Sachin Mourya", "Hitarth Rajpal", "Suganya Selvaraj", "Ujjwal Tiwari"}
        silent = expected - all_authors
        if silent:
            print(f"\n⚠️ SILENT MEMBERS (no updates ever): {', '.join(silent)}")
        
    elif report_type == "leadership":
        print("=" * 50)
        print(f"📊 LEADERSHIP REPORT — {now.strftime('%B %d, %Y')}")
        print("=" * 50)
        
        active_blockers = len([u for u in updates if u["classification"] == "blocker" and not u["resolved"]])
        total_tasks = len(tasks)
        completed_tasks = len([t for t in tasks if t["status"] == "completed"])
        
        # Confidence score (0-100)
        confidence = 100
        confidence -= active_blockers * 15  # Each blocker reduces confidence
        confidence -= len([t for t in tasks if t["status"] == "delayed"]) * 10
        
        # Check milestone proximity risk
        for m in milestones:
            days_left = (datetime.strptime(m["date"], "%Y-%m-%d") - now).days
            if days_left <= 3 and active_blockers > 0:
                confidence -= 20
        
        confidence = max(0, min(100, confidence))
        
        conf_icon = "🟢" if confidence >= 70 else "🟡" if confidence >= 40 else "🔴"
        
        print(f"\n{conf_icon} Project Confidence: {confidence}/100")
        print(f"\n📈 Progress: {completed_tasks}/{total_tasks} tasks complete")
        print(f"🚫 Active Blockers: {active_blockers}")
        
        # Velocity trend
        if len(velocity) >= 7:
            recent_vel = sum(v["tasks_completed"] for v in velocity[-3:]) / 3
            older_vel = sum(v["tasks_completed"] for v in velocity[-7:-3]) / 4
            trend = "📈 Improving" if recent_vel > older_vel else "📉 Declining" if recent_vel < older_vel else "➡️ Stable"
            print(f"📊 Velocity Trend: {trend} ({recent_vel:.1f} vs {older_vel:.1f} tasks/day)")
        
        print(f"\n🎯 Key Risks:")
        for m in milestones:
            days_left = (datetime.strptime(m["date"], "%Y-%m-%d") - now).days
            if days_left <= 7:
                risk = "HIGH" if active_blockers > 0 else "LOW"
                print(f"   [{risk}] {m['name']} in {days_left}d — {active_blockers} blocker(s)")
        
        print("\n" + "=" * 50)

# ============================================================
# MODE: ANALYTICS
# ============================================================

def mode_analytics():
    """Generate execution analytics and pattern insights."""
    updates, _ = github_get("updates.json")
    velocity, _ = github_get("velocity.json")
    tasks, _ = github_get("tasks.json")
    
    now = datetime.now()
    
    print("=" * 50)
    print(f"📊 EXECUTION ANALYTICS — {now.strftime('%B %d, %Y')}")
    print("=" * 50)
    
    # 1. Repeat blocker patterns
    print("\n🔄 Repeat Blocker Analysis:")
    blocker_authors = {}
    for u in updates:
        if u["classification"] == "blocker":
            blocker_authors.setdefault(u["author"], []).append(u)
    
    for author, blockers in sorted(blocker_authors.items(), key=lambda x: -len(x[1])):
        if len(blockers) >= 2:
            print(f"   ⚠️ {author}: {len(blockers)} blockers reported — possible systemic dependency")
    
    # 2. Ownership overload
    print("\n👥 Ownership Load:")
    task_owners = {}
    for t in tasks:
        if t["status"] not in ("completed",):
            task_owners.setdefault(t["owner"], []).append(t)
    
    for owner, owner_tasks in sorted(task_owners.items(), key=lambda x: -len(x[1])):
        icon = "🔴" if len(owner_tasks) >= 4 else "🟡" if len(owner_tasks) >= 3 else "🟢"
        print(f"   {icon} {owner}: {len(owner_tasks)} active tasks")
    
    # 3. Velocity trend
    print("\n📈 Velocity Insights:")
    if len(velocity) >= 7:
        week1 = velocity[-7:-3] if len(velocity) >= 7 else velocity[:4]
        week2 = velocity[-3:]
        avg1 = sum(v["tasks_completed"] for v in week1) / len(week1) if week1 else 0
        avg2 = sum(v["tasks_completed"] for v in week2) / len(week2) if week2 else 0
        change = ((avg2 - avg1) / avg1 * 100) if avg1 > 0 else 0
        
        trend = "improving" if change > 10 else "declining" if change < -10 else "stable"
        print(f"   Trend: {trend} ({change:+.0f}%)")
        print(f"   Recent avg: {avg2:.1f} tasks/day")
        print(f"   Prior avg: {avg1:.1f} tasks/day")
    else:
        print("   Insufficient data (need 7+ days)")
    
    # 4. Blocker resolution time
    print("\n⏱️ Blocker Resolution:")
    resolved_blockers = [u for u in updates if u["classification"] == "blocker" and u["resolved"]]
    unresolved_blockers = [u for u in updates if u["classification"] == "blocker" and not u["resolved"]]
    print(f"   Resolved: {len(resolved_blockers)}")
    print(f"   Unresolved: {len(unresolved_blockers)}")
    for ub in unresolved_blockers:
        age = (now - datetime.fromisoformat(ub["timestamp"])).total_seconds() / 3600
        status = "🔴 STALE" if age > 48 else "🟡 Active"
        print(f"   {status} {ub['author']}: {ub['message'][:50]}... ({int(age)}h)")
    
    print("\n" + "=" * 50)

# ============================================================
# HELPER: Create GitHub Issue
# ============================================================

def create_github_issue(message, author):
    """Create a GitHub issue for a blocker."""
    payload = json.dumps({
        "title": f"[Blocker] {message[:60]}",
        "body": f"**Reported by:** {author}\n**Time:** {datetime.now().isoformat()}\n\n{message}",
        "labels": ["blocker", "auto-created"]
    })
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    cmd = ["curl", "-s", "-X", "POST",
           "-H", f"Authorization: token {TOKEN}",
           "-H", "Accept: application/vnd.github.v3+json",
           "-d", payload, url]
    if PROXY:
        cmd = ["curl", "-s", "--proxy", PROXY, "-X", "POST"] + cmd[3:]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
        return data.get("number")
    except:
        return None

# ============================================================
# MAIN ENTRY POINT
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: update_board.py <mode> [args...]")
        print("Modes: update, digest, escalate, report, task-extract, analytics, velocity")
        sys.exit(1)
    
    mode = sys.argv[1].lower()
    
    if mode == "update" and len(sys.argv) >= 5:
        mode_update(sys.argv[2], sys.argv[3], sys.argv[4])
    elif mode == "digest":
        mode_digest()
    elif mode == "escalate":
        mode_escalate()
    elif mode == "report" and len(sys.argv) >= 3:
        mode_report(sys.argv[2])
    elif mode == "task-extract" and len(sys.argv) >= 4:
        classification = sys.argv[4] if len(sys.argv) >= 5 else None
        mode_task_extract(sys.argv[2], sys.argv[3], classification)
    elif mode == "analytics":
        mode_analytics()
    elif mode == "velocity":
        mode_velocity_update()
    else:
        print(f"Unknown mode or missing args: {mode}")
        print("Modes: update, digest, escalate, report, task-extract, analytics, velocity")
        sys.exit(1)
