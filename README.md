# Assessment Architect ⚡

A Streamlit-powered automation tool that reads assessment configurations from a CSV file and automatically populates them into an online assessment portal using Selenium browser automation.

---

## 🚀 Features

- **CSV-driven configuration** — Define single or multi-section assessments in a structured CSV file
- **Browser automation** — Uses Selenium to log in and fill assessment forms automatically
- **Multi-section support** — Handles complex assessments with multiple section types (MCQ, Coding, etc.)
- **Smart polling** — Replaces fixed `time.sleep` delays with condition-based polling for faster and more reliable execution
- **Coding section support** — Configures coding language restrictions and default languages per section
- **Exclusive tags** — Supports tagging question sets with custom exclusive tags
- **Configurable wait times** — Adjustable element wait time via UI slider
- **OTP-based login** — Supports mobile number + OTP authentication flow

---

## 🗂️ Project Structure

```
Config-Generator/
├── app.py              # Main Streamlit application
├── requirements.txt    # Python dependencies
└── packages.txt        # System-level packages (for deployment)
```

---

## 📋 Prerequisites

- Python 3.8+
- Google Chrome browser installed
- ChromeDriver (managed automatically via `webdriver-manager`)

---

## ⚙️ Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/varshaposhala/Config-Generator.git
   cd Config-Generator
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the app**
   ```bash
   streamlit run app.py
   ```

---

## 📄 Dependencies

| Package | Purpose |
|---|---|
| `streamlit` | Web UI framework |
| `pandas` | CSV parsing and data manipulation |
| `selenium` | Browser automation |
| `webdriver-manager` | Auto-manages ChromeDriver installation |

---

## 📝 CSV Format

The tool accepts a CSV file with the following structure for each section block:

| Field | Description |
|---|---|
| `Section Type` | Type of section (e.g., MCQ, Coding) |
| `Name of Section` | Display name for the section |
| `Time Limit (in mins)` | Time allowed for the section |
| `Question Library` | Source library for questions |
| `Topic` | Question topic |
| `Difficulty Level` | Easy / Medium / Hard |
| `Sub Topic` | Sub-category of the topic |
| `Number of Questions` | How many questions to pick |
| `Marks for Each Question` | Marks awarded per question |
| `Exclusive Tags (Optional)` | Comma-separated tags to filter questions |
| `Coding Question Restriction` | Allowed coding languages (for coding sections) |
| `Default Coding Language` | Default language pre-selected for candidates |

Multiple sections are separated by a **blank row** in the CSV.

### Example CSV Layout

```
Section Type,MCQ
Name of Section,Aptitude Round
Time Limit (in mins),30
Question Library,Topin Questions
Topic,Quantitative Aptitude
Difficulty Level,Medium
Sub Topic,Percentages
Number of Questions,10
Marks for Each Question,1

Section Type,Coding
Name of Section,Programming Round
Time Limit (in mins),60
Question Library,My Library
Topic,Data Structures
Difficulty Level,Hard
Sub Topic,Arrays
Number of Questions,2
Marks for Each Question,10
Coding Question Restriction,"Python, Java, C++"
Default Coding Language,Python
```

---

## 🖥️ How to Use

1. **Launch the app** with `streamlit run app.py`
2. **Upload your CSV** file using the file uploader
3. **Enter your portal credentials** — mobile number and OTP
4. **Adjust the element wait time** slider if needed (default: 10 seconds)
5. **Click Generate** — the tool will open a browser, log in, and fill in all assessment configurations automatically

---

## 🔧 Configuration Options

| Setting | Description | Default |
|---|---|---|
| Element Wait Time | Seconds to wait for UI elements to load | 10s |
| Mobile Number | Your portal login mobile number | — |
| OTP | One-time passcode for authentication | — |

---

## 🛠️ Technical Notes

- The app uses **smart polling** utilities (`poll_url_changed`, `poll_element_visible`, etc.) instead of hard-coded sleep delays, making automation faster and more robust.
- React-Select dropdowns are handled with a custom `click_react_select` function that works around common Selenium limitations with JS-rendered dropdowns.
- The CSV parser supports **orphan metadata rows** (e.g., coding language rows that appear after a blank separator) and merges them back into the correct section automatically.

---

## 📦 Deployment

This project includes a `packages.txt` for platform deployments (e.g., Streamlit Cloud) that require system-level packages such as Chromium.

---

## 🤝 Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you'd like to change.

---

## 📃 License

This project is open source. See the repository for details.
