# main.py
import re
import os
import tempfile
from typing import List
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from PyPDF2 import PdfReader

app = FastAPI(title="PDF → JSON Extractor (2 PDFs)")

# Helper function to safely extract regex match
def safe_search(pattern: str, text: str, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None

# Extract all text from a PDF
def extract_text_from_pdf(path: str) -> str:
    try:
        reader = PdfReader(path)
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF: {e}")
    return "\n".join([p.extract_text() or "" for p in reader.pages])

# Extract JSON from a single PDF
def extract_agreement_data(pdf_path: str):
    text = extract_text_from_pdf(pdf_path)
    if not text:
        raise ValueError("No text could be extracted from the PDF.")

    txt = re.sub(r"\r", "\n", text)
    txt = re.sub(r"[ \t]+", " ", txt)

    contract_no = safe_search(r"Contract No[:\s]*([A-Z0-9\-\./]+)", txt, re.I)
    date = safe_search(r"Date[:\s]*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", txt, re.I)

    seller = safe_search(r"SELLER\s+([\s\S]+?)\s+CONSIGNEE", txt, re.I)
    consignee = safe_search(r"CONSIGNEE\s+([\s\S]+?)\s+NOTIFY PARTY", txt, re.I)
    notify_party = safe_search(r"NOTIFY PARTY\s+([\s\S]+?)\s+Product", txt, re.I) or safe_search(r"NOTIFY PARTY\s+([\s\S]+)$", txt, re.I)

    product_name = product_quantity = product_price = product_amount = None
    prod_match = re.search(
        r"([A-Za-z &-]+?)\s+([0-9,]+\s*bales|[0-9,]+)\s+([\d,]+\s*USD\/bale|[\d,]+\s*USD\/\w+|[\d,]+\s*USD)\s+([\d,\,]+\s*USD)",
        txt,
        re.I,
    )
    if prod_match:
        product_name = prod_match.group(1).strip()
        product_quantity = prod_match.group(2).strip()
        product_price = prod_match.group(3).strip()
        product_amount = prod_match.group(4).strip()

    packing = safe_search(r"Packing[:\s]*([^\n]+)", txt, re.I)
    loading_port = safe_search(r"Loading Port[:\s]*([^\n]+)", txt, re.I)
    destination_port = safe_search(r"Destination Port[:\s]*([^\n]+)", txt, re.I)
    shipment_date = safe_search(r"Shipment[:\s]*([^\n]+)", txt, re.I)
    seller_bank = safe_search(r"Seller(?:’|')?s Bank[:\s]*([^\n]+)", txt, re.I)
    account_no = safe_search(r"Account No\.?:\s*([0-9\-\s]+)", txt, re.I)
    documents_raw = safe_search(r"Documents[:\s]*([^\n]+)", txt, re.I)
    documents = [d.strip() for d in documents_raw.split(",")] if documents_raw else []
    payment_terms = safe_search(r"Payment Terms[:\s]*([^\n]+)", txt, re.I)

    detailed_json = {
        "header": {
            "contract_no": contract_no,
            "date": date,
        },
        "company": {
            "website": safe_search(r"Website[:\s]*([^\n]+)", txt, re.I),
            "email": safe_search(r"Email[:\s]*([^\n]+)", txt, re.I),
            "company_name": safe_search(r"([A-Z][A-Z0-9 &]*PVT LTD)", txt, re.M),
            "address": safe_search(r"Address[:\s]*([^\n]+)", txt, re.I),
            "gst": safe_search(r"GST[:\s]*([^\n]+)", txt, re.I) or safe_search(r"GSTIN([0-9A-Z]+)", txt, re.I),
        },
        "parties": {
            "seller": seller,
            "consignee": consignee,
            "notify_party": notify_party,
        },
        "product": {
            "name": product_name,
            "quantity": product_quantity,
            "price_per_unit": product_price,
            "amount_total": product_amount,
            "packing": packing,
        },
        "shipment": {
            "loading_port": loading_port,
            "destination_port": destination_port,
            "shipment_date": shipment_date,
        },
        "bank_details": {
            "seller_bank": seller_bank,
            "account_no": account_no,
        },
        "documents": documents,
        "payment_terms": payment_terms,
    }

    return detailed_json

# Merge two JSONs, preferring non-empty values
def merge_jsons(json1, json2):
    merged = {}
    for key in json1:
        if isinstance(json1[key], dict) and isinstance(json2.get(key), dict):
            merged[key] = merge_jsons(json1[key], json2[key])
        else:
            merged[key] = json1[key] or json2.get(key)
    return merged

# Upload and extract 2 PDFs separately
@app.post("/upload-two-pdfs/")
async def upload_two_pdfs(files: List[UploadFile] = File(...)):
    if len(files) != 2:
        raise HTTPException(status_code=400, detail="Please upload exactly 2 PDF files (Proposal & Agreement).")

    results = {}
    for file in files:
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"{file.filename} is not a PDF file.")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        try:
            content = await file.read()
            tmp.write(content)
            tmp.flush()
            tmp.close()
            data = extract_agreement_data(tmp.name)
            results[file.filename] = data
        finally:
            os.remove(tmp.name)

    return JSONResponse(content={"pdfs_json": results})

# Upload 2 PDFs and return merged JSON
@app.post("/upload-two-pdfs/match/")
async def upload_and_match(files: List[UploadFile] = File(...)):
    if len(files) != 2:
        raise HTTPException(status_code=400, detail="Upload exactly 2 PDFs.")

    pdf_jsons = []
    for file in files:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        try:
            content = await file.read()
            tmp.write(content)
            tmp.flush()
            tmp.close()
            data = extract_agreement_data(tmp.name)
            pdf_jsons.append(data)
        finally:
            os.remove(tmp.name)

    matched_json = merge_jsons(pdf_jsons[0], pdf_jsons[1])

    return JSONResponse(content={"matched_json": matched_json})
