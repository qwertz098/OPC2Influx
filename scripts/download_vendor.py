"""
Download vendored JS libraries for offline operation.
Run once: python scripts/download_vendor.py
"""
import pathlib
import urllib.request

VENDOR = pathlib.Path(__file__).parent.parent / "static" / "vendor"
VENDOR.mkdir(parents=True, exist_ok=True)

LIBS = [
    (
        "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js",
        "htmx@2.0.4.min.js",
    ),
    (
        "https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js",
        "echarts@5.min.js",
    ),
]

for url, name in LIBS:
    dest = VENDOR / name
    if dest.exists():
        print(f"  {name} already present, skipping.")
    else:
        print(f"  Downloading {name} ...")
        urllib.request.urlretrieve(url, dest)
        print(f"  Saved to {dest}")

print("Done.")
