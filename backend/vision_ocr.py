import logging
import cv2
import numpy as np
import re

logger = logging.getLogger(__name__)

import sys
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Try importing EasyOCR globally
reader = None
try:
    import easyocr
except ImportError:
    logger.error("EasyOCR is not currently installed or available in this Python environment.")

def get_ocr_reader():
    global reader
    if reader is None:
        try:
            import easyocr
            reader = easyocr.Reader(['en'], gpu=False, verbose=False)
        except Exception as e:
            logger.error(f"Failed to initialize EasyOCR: {e}")
            reader = False
    return reader if reader is not False else None

def detect_text(image_bytes: bytes) -> tuple[str, list]:
    r = get_ocr_reader()
    if r is None:
        raise Exception("The EasyOCR module is missing or could not be initialized.")

    try:
        # 1. Read image as bytes
        # 2. Convert to numpy array using OpenCV
        np_arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        
        if img is None:
            raise ValueError("Failed to parse image bytes.")

        # --- PREPROCESSING ---
        # 3. Convert image to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # 4. Apply thresholding to improve handwritten detection
        # Adaptive Thresholding cleanly borders handwriting strokes against white paper
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )

        # --- OCR LOGIC ---
        # 5. Extract text using reader.readtext()
        # detail=0 returns just the text string list directly instead of complex tuple coords
        results = r.readtext(thresh, detail=0)

        if not results:
            return "", []

        # 6. Combine all detected text into a single string
        full_text = " ".join(results)

        # --- EXTRA: REGEX NUMBERS ---
        # Use regex to extract only numbers safely
        # E.g. splits strictly valid numbers 1-250
        raw_numbers = re.findall(r'\d+', full_text)
        
        # Remove duplicate numbers and sort
        unique_numbers = sorted(list(set([int(n) for n in raw_numbers if 0 < int(n) <= 250])))

        return full_text, unique_numbers

    except Exception as e:
        logger.error(f"EasyOCR detection failed: {str(e)}")
        raise e
