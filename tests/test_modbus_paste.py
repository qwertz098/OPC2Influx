"""Register lists pasted from an AI answer or a spreadsheet."""
import pytest

from app.modbus.table_import import normalize, parse_rows

MD = """Here is the register map:

```
| Name | Register | Datentyp | Faktor | Einheit | Beschreibung |
|------|----------|----------|--------|---------|--------------|
| Vorlauftemperatur | 40001 | INT | 0,1 | °C | flow temp |
| Leistung | 40003 | REAL (CDAB) | | kW | |
| Störung | 40010.3 | BOOL | | | fault bit |
| Pumpe | 00005 | BOOL | | | |
| Druck | 30002 | UINT | 1/100 | bar | |
| Zähler | 0x0100 | UDINT | | kWh | |
```
"""


def _all(text, **kw):
    return [normalize(r, **kw) for r in parse_rows(text)]


def test_markdown_from_ai():
    rows = _all(MD)
    assert rows == [
        {"name": "Vorlauftemperatur", "area": "holding", "address": 0, "type": "int16", "scale": 0.1},
        {"name": "Leistung", "area": "holding", "address": 2, "type": "float32", "order": "CDAB"},
        {"name": "Störung", "area": "holding", "address": 9, "type": "uint16", "bit": "3"},
        {"name": "Pumpe", "area": "coil", "address": 4, "type": "bool"},
        {"name": "Druck", "area": "input", "address": 1, "type": "uint16", "scale": 0.01},
        {"name": "Zähler", "area": "holding", "address": 256, "type": "uint32"},
    ]


def test_tsv_semicolon_json():
    tsv = "Bezeichnung\tAdresse\tTyp\tBereich\nT1\t5\tfloat\tHolding Register\nIn1\t2\tbit\tDiscrete Input\n"
    assert _all(tsv) == [{"name": "T1", "area": "holding", "address": 5, "type": "float32"},
                         {"name": "In1", "area": "discrete", "address": 2, "type": "bool"}]
    semi = "name;address;type;scale\nA;10;DINT;0,5\n"
    assert _all(semi) == [{"name": "A", "area": "holding", "address": 10, "type": "int32", "scale": 0.5}]
    js = '[{"name": "B", "address": 7, "type": "uint32", "order": "little endian", "area": "FC4"}]'
    assert _all(js) == [{"name": "B", "area": "input", "address": 7, "type": "uint32", "order": "DCBA"}]


def test_modicon_off_and_errors():
    assert _all("name,address\nX,40001\n", modicon=False)[0]["address"] == 40001
    with pytest.raises(ValueError, match="type: unknown"):
        _all("name,address,type\nX,1,banana\n")
    with pytest.raises(ValueError, match="address: missing"):
        _all("name,address\nX,\n")
    with pytest.raises(ValueError, match="needs a bit"):
        _all("name,address,type,area\nX,1,bool,holding\n")
    assert _all("address,type\n40005,uint16\n")[0]["name"] == "HR4"          # no name: from the register


@pytest.mark.asyncio
async def test_paste_routes(async_client, tmp_config_dir):
    import yaml
    c = async_client
    r = await c.post("/config/servers", data={"protocol": "modbus", "display_name": "Dev", "mb_host": "10.0.0.9"},
                     follow_redirects=False)
    base = r.headers["location"]
    assert "Markdown table" in (await c.get(base + "/ai-prompt")).text
    text = MD + "\n"
    r = await c.post(base + "/paste/preview", json={"text": text})
    rows = r.json()["rows"]
    assert [x["status"] for x in rows] == ["new"] * 6 and rows[2]["node_id"] == "hr9.3"
    r = await c.post(base + "/paste", json={"text": text})
    assert r.json() == {"added": 6, "updated": 0, "errors": []}
    # again with a changed scale and a clash: update + error
    text2 = "| name | address | type | scale |\n|---|---|---|---|\n| Vorlauftemperatur | 40001 | INT | 0.5 |\n| Other | 40003 | REAL | |\n"
    rows = (await c.post(base + "/paste/preview", json={"text": text2})).json()["rows"]
    assert rows[0]["status"] == "update" and rows[1]["status"] == "new"      # hr2:float32 ≠ hr2:float32:CDAB
    text3 = "| name | address | type | scale |\n|---|---|---|---|\n| Dup | 40001 | INT | 0,1 |\n"
    rows = (await c.post(base + "/paste/preview", json={"text": text3})).json()["rows"]
    assert rows[0]["status"] == "error" and "already configured" in rows[0]["error"]
    r = await c.post(base + "/paste", json={"text": text2})
    assert r.json() == {"added": 1, "updated": 1, "errors": []}
    subs = {s["display_name"]: s for s in yaml.safe_load((tmp_config_dir / "subscriptions.yaml").read_text())["subscriptions"]}
    assert subs["Vorlauftemperatur"]["modbus"]["scale"] == 0.5 and len(subs) == 7
