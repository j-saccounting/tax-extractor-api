import boto3
import io
import csv
import re
from flask import Flask, request, jsonify
from pdf2image import convert_from_bytes

app = Flask(__name__)
app.json.sort_keys = False

textract = boto3.client("textract", region_name="us-east-1")


# -----------------------------------
# TEXTRACT
# -----------------------------------
def call_textract(image_bytes):
    return textract.analyze_document(
        Document={"Bytes": image_bytes},
        FeatureTypes=["TABLES"]
    )


# -----------------------------------
# PDF → IMAGE
# -----------------------------------
def to_images(file_bytes, filename):
    if filename.lower().endswith(".pdf"):
        pages = convert_from_bytes(file_bytes)

        images = []

        for page in pages:
            buf = io.BytesIO()
            page.save(buf, format="PNG")
            images.append(buf.getvalue())

        return images

    return [file_bytes]


# -----------------------------------
# TABLE EXTRACTION
# -----------------------------------
def extract_tables(textract_result):
    blocks = textract_result["Blocks"]
    block_map = {b["Id"]: b for b in blocks}

    tables = []

    for block in blocks:
        if block["BlockType"] != "TABLE":
            continue

        table = []

        for rel in block.get("Relationships", []):
            if rel["Type"] != "CHILD":
                continue

            for cid in rel["Ids"]:
                cell = block_map.get(cid)

                if not cell:
                    continue

                if cell["BlockType"] != "CELL":
                    continue

                text = ""

                for r in cell.get("Relationships", []):
                    if r["Type"] == "CHILD":
                        for wid in r["Ids"]:
                            w = block_map.get(wid)

                            if w and "Text" in w:
                                text += w["Text"] + " "

                table.append({
                    "row": cell["RowIndex"],
                    "col": cell["ColumnIndex"],
                    "text": text.strip()
                })

        tables.append(table)

    return tables


# -----------------------------------
# GRID
# -----------------------------------
def table_to_grid(table):
    max_r = max(c["row"] for c in table)
    max_c = max(c["col"] for c in table)

    grid = [["" for _ in range(max_c)] for _ in range(max_r)]

    for c in table:
        grid[c["row"] - 1][c["col"] - 1] = c["text"]

    return grid


# -----------------------------------
# VALUE EXTRACTION
# -----------------------------------
def extract_number(text):
    text = text.replace(",", "").strip()

    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)

    return None


# -----------------------------------
# W2 PARSER
# -----------------------------------
from collections import OrderedDict

def extract_w2(tables):

    result = OrderedDict({
        "wages_box1": None,
        "federal_withholding_box2": None,
        "ss_wages_box3": None,
        "ss_tax_box4": None,
        "medicare_wages_box5": None,
        "medicare_tax_box6": None,
    })

    for table in tables:

        grid = table_to_grid(table)

        for i in range(len(grid) - 1):

            header = " ".join(grid[i]).lower()
            values = grid[i + 1]

            nums = [extract_number(v) for v in values]
            nums = [n for n in nums if n is not None]

            # -----------------------------------
            # BOX 1 / BOX 2
            # -----------------------------------
            if (
                "wages" in header
                and "federal" in header
                and result["wages_box1"] is None
            ):

                if len(nums) >= 2:
                    result["wages_box1"] = nums[0]
                    result["federal_withholding_box2"] = nums[1]

            # -----------------------------------
            # BOX 3 / BOX 4
            # IMPORTANT:
            # avoid matching "social security tips"
            # -----------------------------------
            if (
                "social security wages" in header
                and "tips" not in header
                and result["ss_wages_box3"] is None
            ):

                if len(nums) >= 2:
                    result["ss_wages_box3"] = nums[0]
                    result["ss_tax_box4"] = nums[1]

            # -----------------------------------
            # BOX 5 / BOX 6
            # -----------------------------------
            if (
                "medicare wages" in header
                and result["medicare_wages_box5"] is None
            ):

                if len(nums) >= 2:
                    result["medicare_wages_box5"] = nums[0]
                    result["medicare_tax_box6"] = nums[1]

    return result

# -----------------------------------
# API ENDPOINT
# -----------------------------------
@app.route("/extract-w2", methods=["POST"])
def extract_w2_api():

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]

    file_bytes = file.read()

    images = to_images(file_bytes, file.filename)

    all_tables = []

    for img in images:
        textract_result = call_textract(img)
        tables = extract_tables(textract_result)
        all_tables.extend(tables)

    result = extract_w2(all_tables)

    return jsonify(result)


# -----------------------------------
# HEALTH CHECK
# -----------------------------------
@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "service": "tax-extractor-api"
    })


# -----------------------------------
# RUN
# -----------------------------------
if __name__ == "__main__":
    app.run(debug=True)