"""
Deacon Bus Live — BODS Proxy + Photo Storage
Fetches real-time bus positions from the UK Bus Open Data Service.
Stores bus photos in PostgreSQL.

Environment variables required:
  BODS_API_KEY  — from data.bus-data.dft.gov.uk
  DATABASE_URL  — auto-set by Railway Postgres add-on
"""

import os
import re
import base64
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
CORS(app)

BODS_URL = "https://data.bus-data.dft.gov.uk/api/v1/datafeed/"

BBOX = {
    "minLon": -2.65,
    "minLat": 53.82,
    "maxLon": -0.10,
    "maxLat": 54.57,
}

SIRI_NS = "http://www.siri.org.uk/siri"

# Max image size ~6MB
MAX_IMAGE_BYTES = 6_000_000


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(db_url, cursor_factory=RealDictCursor)


def init_db():
    """Create photos table if it doesn't exist."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS photos (
                id        SERIAL PRIMARY KEY,
                vehicle   TEXT NOT NULL,
                data      TEXT NOT NULL,
                name      TEXT,
                taken_at  TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_photos_vehicle ON photos(vehicle);
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB init error: {e}")


# ── SIRI-VM parsing ───────────────────────────────────────────────────────────

def g(va, tag):
    el = va.find(f".//{{{SIRI_NS}}}{tag}")
    return el.text.strip() if el is not None and el.text else None


def parse_delay(delay_text):
    if not delay_text:
        return 0
    m = re.match(r"(-?)PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", delay_text)
    if not m:
        return 0
    sign = -1 if m.group(1) == "-" else 1
    h = int(m.group(2) or 0)
    mins = int(m.group(3) or 0)
    secs = int(m.group(4) or 0)
    return round(sign * (h * 60 + mins + secs / 60))


def parse_siri(xml_text):
    buses = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return buses

    for va in root.iter(f"{{{SIRI_NS}}}VehicleActivity"):
        try:
            lat = float(g(va, "Latitude") or 0) or None
            lng = float(g(va, "Longitude") or 0) or None
        except ValueError:
            lat = lng = None

        bearing_text = g(va, "Bearing")
        try:
            bearing = float(bearing_text) if bearing_text else None
        except ValueError:
            bearing = None

        delay_minutes = parse_delay(g(va, "DelayBeforeNextStop") or g(va, "Delay"))

        buses.append({
            "lineRef":      g(va, "LineRef"),
            "vehicleRef":   g(va, "VehicleRef"),
            "lat":          lat,
            "lng":          lng,
            "bearing":      bearing,
            "destination":  g(va, "DestinationName") or g(va, "DirectionRef"),
            "origin":       g(va, "OriginName"),
            "operatorRef":  g(va, "OperatorRef"),
            "recordedAt":   g(va, "RecordedAtTime"),
            "delayMinutes": delay_minutes,
        })

    return buses


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    db_ok = False
    try:
        conn = get_db()
        conn.close()
        db_ok = True
    except Exception:
        pass
    return jsonify({"ok": True, "db": db_ok})


@app.route("/api/buses")
def buses():
    api_key = os.environ.get("BODS_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "BODS_API_KEY not set"}), 500

    params = {
        "api_key":     api_key,
        "boundingBox": f"{BBOX['minLon']},{BBOX['minLat']},{BBOX['maxLon']},{BBOX['maxLat']}",
    }

    try:
        resp = requests.get(BODS_URL, params=params, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    bus_list = parse_siri(resp.text)
    return jsonify({"ok": True, "buses": bus_list, "count": len(bus_list)})


@app.route("/api/photos/<vehicle>", methods=["GET"])
def get_photos(vehicle):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, data, name, taken_at FROM photos WHERE vehicle = %s ORDER BY taken_at ASC",
            (vehicle,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        photos = [
            {
                "id":      row["id"],
                "data":    row["data"],
                "name":    row["name"],
                "date":    row["taken_at"].isoformat() if row["taken_at"] else None,
            }
            for row in rows
        ]
        return jsonify({"ok": True, "photos": photos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/photos/<vehicle>", methods=["POST"])
def add_photo(vehicle):
    body = request.get_json(silent=True) or {}
    data = body.get("data", "")
    name = body.get("name", "photo")

    if not data:
        return jsonify({"ok": False, "error": "No image data"}), 400
    if len(data) > MAX_IMAGE_BYTES:
        return jsonify({"ok": False, "error": "Image too large (max ~5MB)"}), 413

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO photos (vehicle, data, name) VALUES (%s, %s, %s) RETURNING id, taken_at",
            (vehicle, data, name)
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({
            "ok":   True,
            "id":   row["id"],
            "date": row["taken_at"].isoformat(),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/photos/<vehicle>/<int:photo_id>", methods=["DELETE"])
def delete_photo(vehicle, photo_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM photos WHERE id = %s AND vehicle = %s",
            (photo_id, vehicle)
        )
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        if deleted == 0:
            return jsonify({"ok": False, "error": "Photo not found"}), 404
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5111))
    app.run(host="0.0.0.0", port=port)

init_db()
