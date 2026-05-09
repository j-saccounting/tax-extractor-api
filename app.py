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

def extract_1099_int(tables):

    result = {}

    total_interest = 0.0

    for table in tables:

        headers = {}

        # -----------------------------------
        # MAP HEADERS
        # -----------------------------------
        for cell in table:
            print(cell)

            text = cell["text"].lower()

            if "interest income" in text:
                headers["interest"] = cell["col"]

            if "early withdrawal penalty" in text:
                headers["penalty"] = cell["col"]

            if "fed income tax withheld" in text:
                headers["federal"] = cell["col"]

        # -----------------------------------
        # EXTRACT ROW VALUES
        # -----------------------------------
        for cell in table:

            row = cell["row"]
            col = cell["col"]
            text = cell["text"]

            # Skip header row
            if row == 1:
                continue

            try:

                value = float(
                    text.replace("$", "").replace(",", "")
                )

            except:
                continue

            # -----------------------------------
            # INTEREST INCOME
            # -----------------------------------
            if col == headers.get("interest"):

                total_interest = value

            # -----------------------------------
            # FEDERAL WITHHOLDING
            # -----------------------------------
            if col == headers.get("federal"):

                result["federal_withholding_box4"] = value

            # -----------------------------------
            # EARLY WITHDRAWAL PENALTY
            # -----------------------------------
            if col == headers.get("penalty"):

                result["early_withdrawal_penalty_box2"] = value

    result["interest_income_box1"] = round(total_interest, 2)

    return result

def extract_1099_div(textract_result):

    import re

    result = {}

    lines = []

    for block in textract_result["Blocks"]:

        if block["BlockType"] == "LINE":

            text = block.get("Text", "")

            lines.append(text)

    full_text = " ".join(lines)

        # -----------------------------------
    # FIND ALL MONEY VALUES
    # -----------------------------------
    amounts = re.findall(
        r"\$([\d,]+\.\d{2})",
        full_text
    )

    amounts = [
        float(a.replace(",", ""))
        for a in amounts
    ]

    # DEBUG
    print("DIV AMOUNTS:", amounts)

    # -----------------------------------
    # BOX 1A — ORDINARY DIVIDENDS
    # -----------------------------------
    ordinary_match = re.search(
        r"1a total ordinary dividends",
        full_text,
        re.IGNORECASE
    )

    if ordinary_match and len(amounts) >= 1:

        result["ordinary_dividends_box1a"] = amounts[0]

    # -----------------------------------
    # BOX 1B — QUALIFIED DIVIDENDS
    # -----------------------------------
    qualified_match = re.search(
        r"1b qualified dividends",
        full_text,
        re.IGNORECASE
    )

    if qualified_match and len(amounts) >= 2:

        result["qualified_dividends_box1b"] = amounts[1]

    # -----------------------------------
    # BOX 2A — CAPITAL GAIN DISTRIBUTIONS
    # -----------------------------------
    capital_match = re.search(
        r"2a total capital gain distr",
        full_text,
        re.IGNORECASE
    )

    if capital_match and len(amounts) >= 3:

        result["capital_gain_distributions_box2a"] = amounts[2]

    # -----------------------------------
    # BOX 4 — FEDERAL WITHHOLDING
    # -----------------------------------
    federal_match = re.search(
        r"4 federal income tax withheld",
        full_text,
        re.IGNORECASE
    )

    if federal_match and len(amounts) >= 4:

        result["federal_withholding_box4"] = amounts[3]

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
def detect_form(textract_result):

    lines = []

    for block in textract_result["Blocks"]:

        if block["BlockType"] == "LINE":
            text = block.get("Text", "").lower()
            lines.append(text)

    full_text = " ".join(lines)
    print(full_text)

    # -----------------------------------
    # W-2
    # -----------------------------------
    if "wage and tax statement" in full_text:
        return "W2"

    # -----------------------------------
    # 1099-INT
    # -----------------------------------
    if "interest income" in full_text:
        return "1099-INT"

    # -----------------------------------
    # 1099-DIV
    # -----------------------------------
    if (
        "form 1099-div" in full_text
        or "qualified dividends" in full_text
        or "total ordinary dividends" in full_text
    ):
        return "1099-DIV"

    # -----------------------------------
    # 1099-R
    # -----------------------------------
    if "distributions from pensions" in full_text:
        return "1099-R"

    # -----------------------------------
    # 1098
    # -----------------------------------
    if "mortgage interest statement" in full_text:
        return "1098"

    return "UNKNOWN"

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

        form_type = detect_form(textract_result)

        # -----------------------------------
        # ROUTE TO PARSER
        # -----------------------------------
        if form_type == "W2":

            result = extract_w2(tables)

        elif form_type == "1099-INT":

            result = extract_1099_int(tables)

        elif form_type == "1099-DIV":

            result = extract_1099_div(textract_result)

        else:

            result = {
                "detected_form": form_type,
                "message": "Parser not implemented yet"
            }

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