import boto3
import io
import csv
import re
import base64
from flask import Flask, request, jsonify, render_template
from pdf2image import convert_from_bytes

app = Flask(__name__)
app.json.sort_keys = False

textract = boto3.client("textract", region_name="us-west-2")


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
def analyze_document(file_bytes):

    return textract.analyze_document(
        Document={
            "Bytes": file_bytes
        },
        FeatureTypes=["TABLES"]
    )
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/extract-w2", methods=["POST"])
def extract_w2_route():

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]

    if f.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    filename = f.filename.lower()

    file_bytes = f.read()

    try:

        # -----------------------------------
        # PDF HANDLING
        # -----------------------------------
        if filename.endswith(".pdf"):

            images = convert_from_bytes(file_bytes)

            if not images:
                return jsonify({"error": "Could not read PDF"}), 400

            img = images[0]

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG")

            textract_bytes = buffer.getvalue()

        else:
            textract_bytes = file_bytes

        # -----------------------------------
        # TEXTRACT
        # -----------------------------------
        textract_result = analyze_document(textract_bytes)

        # -----------------------------------
        # TABLE EXTRACTION
        # -----------------------------------
        tables = extract_tables(textract_result)

        # -----------------------------------
        # W2 PARSING
        # -----------------------------------
        result = extract_w2(tables)

        return jsonify(result)

    except Exception as e:

        print("ERROR:")
        print(str(e))

        return jsonify({
            "error": str(e)
        }), 500

# -----------------------------------
# RUN
# -----------------------------------
if __name__ == "__main__":
    app.run(debug=True)