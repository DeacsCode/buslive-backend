"""
Deacon Bus Live — BODS Proxy
Fetches real-time bus positions from the UK Bus Open Data Service.
Set BODS_API_KEY as an environment variable in Railway.
"""

import os
import re
import requests
import xml.etree.ElementTree as ET
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BODS_URL = "https://data.bus-data.dft.gov.uk/api/v1/datafeed/"

BBOX = {
    "minLon": -1.25,
    "minLat": 53.88,
    "maxLon": -0.95,
    "maxLat": 54.05,
}

SIRI_NS = "http://www.siri.org.uk/siri"


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


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


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
        resp = requests.get(BODS_URL, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    bus_list = parse_siri(resp.text)
    return jsonify({"ok": True, "buses": bus_list, "count": len(bus_list)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5111))
    app.run(host="0.0.0.0", port=port)
