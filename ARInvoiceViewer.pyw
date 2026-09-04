from tkinter import ttk, messagebox, scrolledtext, font, simpledialog
from tkcalendar import DateEntry
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
import winsound
import os, pymssql, time, threading, re, queue, json, gc, csv

# AR INVOICE DIRECTORY
INVOICE_DIR = r"T:\AR\Sales\ACP\Invoices"
LOG_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_log.csv")
invoice_re  = re.compile(r'(\d+)')

class InvoiceViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Titan AR Invoice Viewer")
        self.iconbitmap(default="icon.ico")
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        
        self.tk.call("tk", "scaling", 1.33)
        self.gui_queue = queue.Queue()
        self.geometry("1400x700")

        self.invoices = [] 
        self.customer_ids = set() 
        self.sort_col = "Date"
        self.sort_desc = True 
        self.current_rows = []
        self.result_count = 0
        self.displayed_count = 0
        self.page_size = 500
        self._loading_page = False
        self.broken_companies = []
        self.broken_invoices = []
        
        self.by_invoice = {}
        self.missing_invoices = []
        self.duplicate_invoices = []

        self.log_usage()

        # Ignore list for private customers
        self.ignoring = True
        self.ignore_list = set()
        if os.path.exists("ignore.json"):
            with open("ignore.json", "r") as f:
                self.ignore_list = set(json.load(f))
        
        self.protocol("WM_DELETE_WINDOW", self.on_exit)

        # Loading info
        self.startup_sound()
        self.create_loading_screen()

        # Get data
        self.after(0, lambda: threading.Thread(target=self.load_data, daemon=True).start())
        self.loading_loop_id = self.after(50, self.loading_loop)


    def startup_sound(self):
        winsound.PlaySound("owin31", winsound.SND_ALIAS | winsound.SND_ASYNC)


    def create_loading_screen(self):
        self.loading_bg = tk.PhotoImage(file="logo.png")
        self.loading_canvas = tk.Canvas(self, bg="white", width=1220, height=700)
        self.loading_canvas.pack(expand=True, fill="both", side="top", anchor="w")
        self.loading_canvas.background = self.loading_bg
        self.loading_canvas.create_image(1220/2, 0, anchor="n", image=self.loading_bg)
        
        tk.Label(self.loading_canvas, text="Welcome to Titan AR Invoice Viewer", font=("TKDefaultFont", 24, "bold"), bg="white").pack(side="top", anchor="w")
        tk.Label(self.loading_canvas, text="Please press the Help button for more information about this program", font=("TKDefaultFont", 20), bg="white").pack(side="top", anchor="w")
        tk.Label(self.loading_canvas, text="Loading Titan AR Invoices, Please wait...", font=("TKDefaultFont", 16), bg="white").pack(side="top", anchor="w")


    def loading_update(self, msg, color="#000000"):
        self.gui_queue.put((msg, color))


    def loading_loop(self):
        try:
            while True:
                msg, color = self.gui_queue.get_nowait()
                tk.Label(self.loading_canvas, text=msg, font=("TKDefaultFont", 16), fg=color, bg="white").pack(side="top", anchor="w")
        except queue.Empty:
            pass
        self.loading_loop_id = self.after(50, self.loading_loop)


    def load_data(self):
        self.t0 = time.perf_counter()

        with ThreadPoolExecutor(max_workers=2) as pool:
            _ = pool.submit(self.load_database)
            file_index = pool.submit(self.load_files)

            file_index = file_index.result()
            _ = _.result()

        # Match files with invoices
        t0 = time.perf_counter()
        matched = set()
        for row in self.invoices:
            invoice = str(row["InvoiceNum"]).strip()
            filepath = file_index.get(invoice)
            if filepath:
                row["Filepath"] = filepath
                matched.add(invoice)
            else:
                self.missing_invoices.append((row["CustomerID"], row["InvoiceNum"], row["InvoiceDate"].strftime("%m-%d-%Y")))
        t1 = time.perf_counter()
        self.loading_update(f"Invoice files loaded in {t1 - t0:.2f} seconds.")
        self.loading_update((f"{len(self.broken_companies)} broken titan entries found."), color="#FF0000")
        self.loading_update(f"{len(self.missing_invoices)} missing invoice files.", color="#FF0000")

        # Check for errors
        unmatched = [path for inv, path in file_index.items() if inv not in matched]
        if unmatched:
            self.broken_invoices.extend(unmatched)
            self.loading_update(f"{len(unmatched)} invoice files without matches.", color="#FF0000")

        self.after(100, self.load_gui)

    
    def load_gui(self):
        self.after_cancel(self.loading_loop_id)
        self.loading_canvas.destroy()

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.create_treeview()
        self.create_filter_frame()
        self.create_summary_bar()
        self.error_popup = ErrorPopup(self, self.broken_companies, self.broken_invoices, self.missing_invoices, self.duplicate_invoices)
        self.help_popup = HelpPopup(self)

        self.ignore_photo = tk.PhotoImage(file="leaf.png")
        self.ignore_label = tk.Label(self.filter_frame, image=self.ignore_photo)
        self.bind("<Control-F9>", self.add_ignore)
        self.bind("<Control-F10>", self.toggle_ignore_list)
        
        # Restore filters on refresh
        if hasattr(self, "saved_filters") and self.saved_filters:
            self.all_companies.set(self.saved_filters["all_companies"])
            self.search_names.set(self.saved_filters["search_names"])
            self.pdf_only.set(self.saved_filters["pdf_only"])
            
            self.start_entry.set_date(self.saved_filters["start_date"])
            self.end_entry.set_date(self.saved_filters["end_date"])
            
            self.invoice_text.set(self.saved_filters["invoice"])
            self.search_invoice_names.set(self.saved_filters["search_invoice_names"])

            self.sort_col
            
            # Sorting state
            self.sort_col = self.saved_filters.get("sort_col", "Date")
            self.sort_desc = self.saved_filters.get("sort_desc", True)
            
            if hasattr(self, "customer_entry"):
                # 1. Remove the active trace to stop it from firing while we insert text
                self.customer_entry.customer.trace_remove("write", self.customer_entry.text_trace)
                
                # 2. Insert the saved text
                self.customer_entry.insert(0, self.saved_filters["search_target"])
                
                # 3. Apply the correct trace depending on the 'All Companies' checkbox state
                if self.all_companies.get():
                    self.customer_entry.text_trace = self.customer_entry.customer.trace_add(
                        "write", lambda *_: self.customer_entry.debounced_select(source="customer")
                    )
                else:
                    self.customer_entry.text_trace = self.customer_entry.customer.trace_add(
                        "write", self.customer_entry.show_suggestions
                    )
                
                # 4. Trigger the search manually
                self.customer_entry.on_select()
                
            self.saved_filters = None


    def log_usage(self):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username  = os.environ.get("USERNAME",     "unknown")
        computer  = os.environ.get("COMPUTERNAME", "unknown")

        for attempt in range(5):
            try:
                is_new = not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0
                with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if is_new:
                        writer.writerow(["Date/Time", "Username", "Computer"])
                    writer.writerow([timestamp, username, computer])
                return
            except PermissionError:
                time.sleep(0.1 * (attempt + 1))
            except Exception:
                return


    def load_database(self):
        def load_header():
            t0 = time.perf_counter()
            conn = pymssql.connect(server="ACAPP1", user="titan", password="titan", database="titan")

            with conn.cursor(as_dict=True) as cur:
                cur.execute("""
                    SELECT T.CustomerID, T.Ticket AS InvoiceNum, T.InvoiceDate, T.Subtotal, T.Total, T.Payments, 
                           T.County, T.Tax, T.Discounts, T.Closed, C.CompanyName
                    FROM Tickets T
                    JOIN Customers C ON T.CustomerID = C.CustomerID
                """)
                data = cur.fetchall()

            self.invoices = [row for row in data if row["CustomerID"] and row["InvoiceNum"] and row["InvoiceDate"] 
                            and row["Subtotal"] is not None]
            
            seen_invoices = set()
            for row in self.invoices:
                key = str(row["InvoiceNum"]).strip()
                if key in seen_invoices:
                    self.duplicate_invoices.append(f"{row['CustomerID']} - {row['InvoiceNum']}")
                else:
                    seen_invoices.add(key)
                    
            self.broken_companies = [row for row in data if not (row["CustomerID"] and row["InvoiceNum"] and row["InvoiceDate"] 
                                     and row["Subtotal"] is not None)]
            self.customer_ids = {(row["CustomerID"], row["CompanyName"], row["CustomerID"] in self.ignore_list) for row in self.invoices if row["CustomerID"] and row["CompanyName"]}
            self.by_invoice = {str(row["InvoiceNum"]).strip(): row for row in self.invoices}

            t1 = time.perf_counter()
            self.loading_update(f"AR Invoice data loaded in {t1 - t0:.2f} seconds.")
            conn.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            _ = pool.submit(load_header).result()
        t1 = time.perf_counter()
        self.loading_update(f"Database loaded in {t1 - self.t0:.2f} seconds.")


    def load_files(self):
        t0 = time.perf_counter()
        file_index = {}

        def scan(directory):
            for entry in os.scandir(directory):
                if entry.is_dir():
                    scan(entry.path)      # year folder — go one level deeper
                    continue
                stem = os.path.splitext(entry.name)[0]   # mm-dd-yy_invoice #
                parts = stem.split("_", 1)
                if len(parts) < 2:
                    continue
                invoice = parts[1].replace("[slash]", "/").replace("[quote]", '"').strip()
                if not invoice:
                    continue
                if invoice in file_index:
                    self.broken_invoices.append(f"Duplicate file: {entry.path}")
                    continue
                file_index[invoice] = entry.path

        if os.path.exists(INVOICE_DIR):
            scan(INVOICE_DIR)

        t1 = time.perf_counter()
        self.loading_update(f"Invoice files scanned in {t1 - t0:.2f} seconds.")
        return file_index


    def create_filter_frame(self):
        self.filter_frame = ttk.Frame(self, height=60)
        self.filter_frame.grid(row=0, column=0, sticky="ew")
        self.filter_frame.grid_propagate(False)

        # Row 0
        ttk.Label(self.filter_frame, text="Customer ID:").grid(row=0, column=0, padx=5)
        self.customer_entry = AutoCompleteEntry(self)
        self.customer_entry.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(self.filter_frame, text="Date Range:").grid(row=0, column=2, padx=5)
        self.start_entry = DateEntry(self.filter_frame, width=10, date_pattern="mm/dd/yyyy")
        self.start_entry.set_date("01/01/2014")
        self.start_entry.grid(row=0, column=3, padx=5)
        self.start_entry.bind("<<DateEntrySelected>>", self.customer_entry.on_select)
        self.start_entry.bind("<Return>", self.customer_entry.on_select)

        ttk.Label(self.filter_frame, text="to").grid(row=0, column=4, padx=2)
        
        self.end_entry = DateEntry(self.filter_frame, width=10, date_pattern="mm/dd/yyyy")
        self.end_entry.grid(row=0, column=5, padx=5)
        self.end_entry.bind("<Return>", self.customer_entry.on_select)
        self.end_entry.bind("<<DateEntrySelected>>", self.customer_entry.on_select)

        self.all_companies = tk.BooleanVar()
        self.all_companies_cb = ttk.Checkbutton(self.filter_frame, text="View All Customers", variable=self.all_companies, command=self.customer_entry.toggle_all_companies, takefocus=False)
        self.all_companies_cb.grid(row=0, column=6, padx=15)

        self.pdf_only = tk.BooleanVar()
        self.pdf_cb = ttk.Checkbutton(self.filter_frame, text="File Available Only", variable=self.pdf_only, command=self.customer_entry.on_select, takefocus=False)
        self.pdf_cb.grid(row=0, column=7, padx=5)

        # Dynamic spring column to keep buttons pinned right
        self.filter_frame.columnconfigure(9, weight=1)

        self.right_button_frame = ttk.Frame(self.filter_frame)
        self.right_button_frame.grid(row=0, column=11, padx=(0, 15), sticky="e")
        
        self.refresh_button = tk.Button(self.right_button_frame, text="⭮", command=self.restart)
        self.refresh_button.pack(side="left", padx=2)

        self.help_button = tk.Button(self.right_button_frame, text="Help", command=lambda *_: self.help_popup.toggle())
        self.help_button.pack(side="left", padx=2)

        self.errors_button = tk.Button(self.right_button_frame, text="Errors", command=lambda *_: self.error_popup.toggle())
        self.errors_button.pack(side="left", padx=2)

        # Row 1
        ttk.Label(self.filter_frame, text="Invoice:").grid(row=1, column=0, padx=5)
        self.invoice_entry = tk.Entry(self.filter_frame)
        self.invoice_entry.grid(row=1, column=1, padx=5)
        self.invoice_text = tk.StringVar()
        self.prev_invoice_text = ""
        self.invoice_entry["textvariable"] = self.invoice_text
        self.invoice_text.trace_add("write", lambda *_: self.customer_entry.debounced_select(source="invoice"))
        
        self.search_names = tk.BooleanVar()
        self.search_names_cb = ttk.Checkbutton(self.filter_frame, text="Search Names", variable=self.search_names, command=self.customer_entry.on_select, takefocus=False)
        self.search_names_cb.grid(row=1, column=6, padx=15, sticky="w")

        self.clear_button = tk.Button(self.filter_frame, text="Clear Filters", command=self.clear_filters)
        self.clear_button.grid(row=1, column=3, padx=5, sticky="w")

        self.search_invoice_names = tk.BooleanVar()
        self.search_invoice_names_cb = ttk.Checkbutton(self.filter_frame, text="Search Invoice Names", variable=self.search_invoice_names, command=self.customer_entry.on_select, takefocus=False)
        self.search_invoice_names_cb.grid(row=1, column=7, padx=5, sticky="w")


    def create_summary_bar(self):
        self.summary_frame = ttk.Frame(self, relief="groove", borderwidth=1, padding=(6, 4))
        self.summary_frame.grid(row=2, column=0, sticky="ew")
        
        for i in range(5):
            self.summary_frame.columnconfigure(i, weight=1)

        self.amount_label = ttk.Label(self.summary_frame, text="0 invoices found.", font=("TKDefaultFont", 10, "bold"))
        self.amount_label.grid(row=0, column=0, sticky="w", padx=10)

        self.selected_sum = tk.StringVar(value="Selected Total: $0.00")
        self.selected_sum_label = ttk.Label(self.summary_frame, textvariable=self.selected_sum, font=("TKDefaultFont", 10))
        self.selected_sum_label.grid(row=0, column=1, sticky="w")

        self.invoice_total = tk.StringVar(value="Invoice Total: $0.00")
        self.invoice_total_label = ttk.Label(self.summary_frame, textvariable=self.invoice_total, font=("TKDefaultFont", 10, "bold"))
        self.invoice_total_label.grid(row=0, column=2, sticky="w")

        self.payments_total = tk.StringVar(value="Payments Total: $0.00")
        self.payments_total_label = ttk.Label(self.summary_frame, textvariable=self.payments_total, font=("TKDefaultFont", 10, "bold"))
        self.payments_total_label.grid(row=0, column=3, sticky="w", padx=10)

        self.ar_total = tk.StringVar(value="AR Total: $0.00")
        self.ar_total_label = ttk.Label(self.summary_frame, textvariable=self.ar_total, font=("TKDefaultFont", 10, "bold"))
        self.ar_total_label.grid(row=0, column=4, sticky="w", padx=10)


    def create_treeview(self):
        self.tree_frame = ttk.Frame(self, height=600)
        self.tree_frame.grid(row=1, column=0, sticky="nsew")

        self.tree = ttk.Treeview(self.tree_frame, columns=("Customer", "Company Name", "Invoice", "Date", 
                                                           "State", "Subtotal", "Tax", "Discount", "Total", "Payments", 
                                                           "Closed", "File Available", "Filepath"), show='tree headings')
        self.tree.column("#0", width=0, stretch=False)
        self.tree.column("Customer", width=70, anchor="center")
        self.tree.column("Company Name", width=150, anchor="center")
        self.tree.column("Invoice", width=100, anchor="center")
        self.tree.column("Date", width=70, anchor="center")
        self.tree.column("State", width=50, anchor="center")
        self.tree.column("Subtotal", width=70, anchor="center")
        self.tree.column("Tax", width=70, anchor="center")
        self.tree.column("Discount", width=70, anchor="center")
        self.tree.column("Total", width=80, anchor="center")
        self.tree.column("Payments", width=80, anchor="center")
        self.tree.column("Closed", width=60, anchor="center")
        self.tree.column("File Available", width=80, anchor="center")
        self.tree.column("Filepath", width=0, stretch=False)

        self.tree.heading("Customer", text="Customer", command=lambda: self.sort_by("Customer"))
        self.tree.heading("Company Name", text="Company Name", command=lambda: self.sort_by("Company Name"))
        self.tree.heading("Invoice", text="Invoice", command=lambda: self.sort_by("Invoice"))
        self.tree.heading("Date", text="Date  ▼", command=lambda: self.sort_by("Date"))
        self.tree.heading("State", text="State", command=lambda: self.sort_by("State"))
        self.tree.heading("Subtotal", text="Subtotal", command=lambda: self.sort_by("Subtotal"))
        self.tree.heading("Tax", text="Tax", command=lambda: self.sort_by("Tax"))
        self.tree.heading("Discount", text="Discount", command=lambda: self.sort_by("Discount"))
        self.tree.heading("Total", text="Total", command=lambda: self.sort_by("Total"))
        self.tree.heading("Payments", text="Payments", command=lambda: self.sort_by("Payments"))
        self.tree.heading("Closed", text="Closed", command=lambda: self.sort_by("Closed"))
        self.tree.heading("File Available", text="File Available", command=lambda: self.sort_by("File Available"))
        self.tree.heading("Filepath", text="")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree_scrollbar = ttk.Scrollbar(self.tree_frame, command=self.tree.yview)
        self.tree_scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self._on_tree_scroll)
        self.tree.bind("<<TreeviewSelect>>", self.update_selected_sum)
        self.tree.bind("<Double-1>", self.open_file)

        self.style.configure("Treeview", rowheight=20) 
        self.tree.tag_configure("oddrow",  background="#f7f7f7")
        self.tree.tag_configure("evenrow", background="#ffffff")


    def _on_tree_scroll(self, first, last):
        self.tree_scrollbar.set(first, last)
        if self._loading_page or self.displayed_count >= len(self.current_rows):
            return
        try:
            near_bottom = float(last) >= 0.92
        except (TypeError, ValueError):
            return
        if near_bottom:
            self.after_idle(self.load_more_rows)


    def load_more_rows(self, reset=False):
        if self._loading_page:
            return
        self._loading_page = True
        try:
            if reset:
                self.tree.delete(*self.tree.get_children())
                self.displayed_count = 0

            start = self.displayed_count
            end = min(start + self.page_size, len(self.current_rows))
            for i in range(start, end):
                row = self.current_rows[i]
                tag = "evenrow" if i % 2 == 0 else "oddrow"
                self.tree.insert("", "end", values=row, tags=tag)
            self.displayed_count = end
        finally:
            self._loading_page = False
        self.update_result_label()


    def update_result_label(self):
        total = self.result_count
        if total == 0:
            self.amount_label.config(text="No invoices found.")
        elif total == 1:
            self.amount_label.config(text="1 invoice found.")
        elif self.displayed_count < total:
            self.amount_label.config(text=f"{total:,} invoices found. Showing {self.displayed_count:,}.")
        else:
            self.amount_label.config(text=f"{total:,} invoices found.")

    def update_selected_sum(self, *_):
        total = 0
        for item in self.tree.selection():
            amt_str = self.tree.set(item, "Total")
            if amt_str:
                total += float(amt_str.replace("$", "").replace("(", "-").replace(",", "").replace(")", ""))
        total_str = f"${total:,.2f}" if total >= 0 else f"(${abs(total):,.2f})"
        self.selected_sum.set(f"Selected: {total_str}")


    def show_invoices(self, customer_input, invoice_prefix): 
        invoice_count = 0
        invoice_total = 0
        payments_total = 0
        values = []

        search = customer_input.lower()
        invoice_search = invoice_prefix.lower()
        invoice_anywhere = self.search_invoice_names.get()
        search_names = self.search_names.get()
        all_companies = self.all_companies.get()
        pdf_only = self.pdf_only.get()
        ignoring = self.ignoring
        ignore_list = self.ignore_list
        start_date = self.start_entry.get_date()
        end_date = self.end_entry.get_date()

        fmt = lambda x: "$0.00" if not x else (f"${x:,.2f}" if x >= 0 else f"(${abs(x):,.2f})")

        for entry in self.invoices:
            customer = str(entry["CustomerID"])
            if ignoring and customer in ignore_list:
                continue

            company_name = str(entry["CompanyName"])
            customer_l = customer.lower()
            name_match = search_names and bool(search) and search in company_name.lower()
            if all_companies:
                if search and not (customer_l.startswith(search) or name_match):
                    continue
            elif not (customer_l == search or name_match):
                continue

            invoice = str(entry["InvoiceNum"])
            date = entry["InvoiceDate"].date()
            if date < start_date or date > end_date:
                continue

            filepath = entry.get("Filepath", "")
            has_filepath = "✔" if filepath else ""
            if pdf_only and not filepath:
                continue

            inv_l = invoice.lower()
            if invoice_anywhere:
                if invoice_search not in inv_l:
                    continue
            elif not inv_l.startswith(invoice_search):
                continue

            state_val = entry.get('County', "")
            state = str(state_val).strip() if state_val is not None else ""
            sub_amt = entry.get('Subtotal', 0) or 0
            tax_amt = entry.get('Tax', 0) or 0
            disc_amt = entry.get('Discounts', 0) or 0
            tot_amt = entry.get('Total', 0) or 0
            pay_amt = entry.get('Payments', 0) or 0
            is_closed = entry.get('Closed', False)

            invoice_total += tot_amt - disc_amt
            payments_total += pay_amt

            values.append((customer, company_name, invoice, date, state,
                           fmt(sub_amt), fmt(tax_amt), fmt(disc_amt), fmt(tot_amt), fmt(pay_amt),
                           "Yes" if is_closed else "No", has_filepath, filepath))
            invoice_count += 1

        ar_total = invoice_total - payments_total
        self.invoice_total.set(f"Invoice Total: {fmt(invoice_total)}")
        self.payments_total.set(f"Payments Total: {fmt(payments_total)}")
        self.ar_total.set(f"AR Total: {fmt(ar_total)}")
        return invoice_count, values

    def filter_rows(self, customer_input, invoice_prefix):
        company_l = customer_input.lower()
        invoice_search = invoice_prefix.lower()
        invoice_anywhere = self.search_invoice_names.get()
        search_names = self.search_names.get()

        def keep(row):
            company_ok = row[0].lower().startswith(company_l) or (search_names and company_l in row[1].lower())
            inv_l = row[2].lower()
            invoice_ok = invoice_search in inv_l if invoice_anywhere else inv_l.startswith(invoice_search)
            return company_ok and invoice_ok

        self.current_rows = [row for row in self.current_rows if keep(row)]
        self.result_count = len(self.current_rows)

        money = lambda s: float(s.replace("$", "").replace("(", "-").replace(",", "").replace(")", "")) if s else 0.0
        invoice_total = sum(money(row[8]) for row in self.current_rows)
        payments_total = sum(money(row[9]) for row in self.current_rows)
        ar_total = invoice_total - payments_total
        fmt = lambda x: "$0.00" if not x else (f"${x:,.2f}" if x >= 0 else f"(${abs(x):,.2f})")
        self.invoice_total.set(f"Invoice Total: {fmt(invoice_total)}")
        self.payments_total.set(f"Payments Total: {fmt(payments_total)}")
        self.ar_total.set(f"AR Total: {fmt(ar_total)}")

        self.load_more_rows(reset=True)
        return self.result_count

    def clear_filters(self):
        self.customer_entry.delete(0, "end")
        self.invoice_entry.delete(0, "end")
        self.start_entry.set_date("01/01/2014")
        self.end_entry.set_date(datetime.today())
        self.pdf_only.set(False)
        self.show_invoices("", "")


    def sort_by(self, col, values=None, header_pressed=True, watch_cursor=True):
        if header_pressed:
            if col == self.sort_col:
                self.sort_desc = not self.sort_desc
            else:
                self.sort_col = col
                self.sort_desc = True

        if values is not None:
            self.current_rows = list(values)
            self.result_count = len(self.current_rows)

        def invoice_key(inv):
            if inv.isdigit():
                return [(0, int(inv))]
            return [(1,)] + [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in invoice_re.split(inv) if p]

        money = lambda s: float(s.replace("$", "").replace("(", "-").replace(",", "").replace(")", "")) if s else 0
        keymap = {
            "Customer": lambda x: x[0],
            "Company Name": lambda x: x[1],
            "Invoice": lambda x: invoice_key(str(x[2])),
            "Date": lambda x: x[3],
            "State": lambda x: str(x[4]),
            "Subtotal": lambda x: money(x[5]),
            "Tax": lambda x: money(x[6]),
            "Discount": lambda x: money(x[7]),
            "Total": lambda x: money(x[8]),
            "Payments": lambda x: money(x[9]),
            "Closed": lambda x: str(x[10]),
            "File Available": lambda x: x[11]
        }
        reverse = self.sort_desc
        if col in ("Customer", "Invoice", "Company Name"):
            reverse = not reverse

        if watch_cursor:
            self.config(cursor="watch")
            self.tree.config(cursor="watch")
            self.after(25, lambda: self.sort(col, keymap, reverse))
        else:
            self.sort(col, keymap, reverse)


    def sort(self, col, keymap, reverse):
        self.current_rows.sort(key=keymap[col], reverse=reverse)
        self.load_more_rows(reset=True)
        arrow = "  ▼" if self.sort_desc else "  ▲"
        for c in self.tree["columns"]:
            self.tree.heading(c, text=c + arrow if c == col else c)
        self.config(cursor="")
        self.tree.config(cursor="")
        return "break"

    def restart(self):
        try:
            target_entry = getattr(self, "customer_entry", None)
            target_text = target_entry.get() if target_entry else ""
            
            self.saved_filters = {
                "search_target": target_text,
                "invoice": self.invoice_text.get(),
                "start_date": self.start_entry.get_date(),
                "end_date": self.end_entry.get_date(),
                "all_companies": self.all_companies.get(),
                "search_names": self.search_names.get(),
                "pdf_only": self.pdf_only.get(),
                "sort_col": self.sort_col,
                "sort_desc": self.sort_desc,
                "search_invoice_names": self.search_invoice_names.get()
            }
        except Exception:
            self.saved_filters = None

        with open("ignore.json", "w") as f:
            json.dump(list(self.ignore_list), f)
        
        for w in self.winfo_children():
            w.destroy()
        
        self.after_cancel(self.loading_loop_id)
        self.invoices.clear()
        self.customer_ids.clear()
        self.sort_col = "Date"
        self.sort_desc = True
        self.broken_companies.clear()
        self.broken_invoices.clear()
        
        self.by_invoice.clear()
        self.missing_invoices.clear()
        self.duplicate_invoices.clear()

        self.columnconfigure(0, weight=0)
        self.rowconfigure(0, weight=0)

        self.current_rows.clear()
        self.result_count = 0
        self.displayed_count = 0
        self._loading_page = False

        gc.collect()

        self.create_loading_screen()
        self.loading_loop_id = self.after(50, self.loading_loop)
        threading.Thread(target=self.load_data, daemon=True).start()


    def toggle_ignore_list(self, event):
        self.ignoring = not self.ignoring
        if self.ignoring:
            self.ignore_label.grid_forget()
        else:
            self.ignore_label.grid(row=0, column=10, sticky="e", padx=10)
            
        target_entry = getattr(self, "customer_entry", None)
        if target_entry:
            target_entry.on_select()
        return "break"


    def add_ignore(self, event):
        customers = ", ".join([("\n" * ((i) % 7 == 0)) + s for i, s in enumerate(self.ignore_list)])
        new_item = simpledialog.askstring("Hidden Customers", "Current Hidden Customers:\n" + customers + "\n\nEnter new Customer ID (case doesn't matter)", parent=self)
        if new_item:
            self.ignore_list.add(new_item.upper())
        return "break"
    

    def on_exit(self):
        with open("ignore.json", "w") as f:
            json.dump(list(self.ignore_list), f)
        self.destroy()
        
        
    def open_file(self, event):
        try:
            selection = self.tree.selection()
            if not selection:
                return
            
            if self.tree.identify_region(event.x, event.y) != "cell":
                return
        
            row = self.tree.identify_row(event.y)
            if not row:
                return
            
            if selection[0] != row:
                return
            
            item = selection[0]
            filepath = self.tree.set(item, "Filepath")
            if not filepath:
                return
            if not os.path.exists(filepath):
                messagebox.showerror("Error", "File not found.")
                return
            
            os.startfile(filepath)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open file: {e}")


class AutoCompleteEntry(tk.Entry):
    def __init__(self, root: InvoiceViewer, *a, **kw):
        super().__init__(root.filter_frame, *a, **kw)
        self.invoices = root.invoices
        self.customer_ids = root.customer_ids
        self.tree = root.tree
        self.root = root
        self.listbox = None
        self.customer = tk.StringVar()
        self.prev_customer = ""
        self["textvariable"] = self.customer 
        self.search_job = None 

        self.text_trace = self.customer.trace_add("write", self.show_suggestions)
        self.bind("<Return>", self.on_select)
        self.bind("<Up>", lambda *_: self.listbox_move("up"))
        self.bind("<Down>", lambda *_: self.listbox_move("down"))
        self.bind("<Escape>", self.close_listbox)
        self.tree.bind("<Button-3>", self.show_cell_menu, True)


    def show_suggestions(self, *_):
        text = self.customer.get()
        if not text:
            self.close_listbox()
            return

        matches = [w for w in self.customer_ids
                   if (w[0].lower().startswith(text.lower())
                       or (self.root.search_names.get() and text.lower() in w[1].lower()))
                   and (not self.root.ignoring or not w[2])]
        if not matches:
            self.close_listbox()
            return

        if self.listbox is None:
            self.listbox = ttk.Treeview(self.root, columns=("id", "name"), show="tree", height=8)
            self.listbox.heading("id", text="ID")
            self.listbox.heading("name", text="Name")
            self.listbox.bind("<ButtonRelease-1>", self.on_select)
            self.listbox.bind("<Return>", self.on_select)
            self.listbox.bind("<Up>", lambda e: self.listbox_move("up"))
            self.listbox.bind("<Down>", lambda e: self.listbox_move("down"))

        self.listbox.delete(*self.listbox.get_children())
        matches.sort()  
        for w in matches:
            self.listbox.insert("", tk.END, values=(w[0], w[1]))

        x = self.winfo_x()
        y = self.winfo_y() + self.winfo_height() + 7
        self.listbox.place(x=x, y=y)


    def toggle_all_companies(self):
        if self.root.all_companies.get():
            self.customer.trace_remove("write", self.text_trace)
            self.text_trace = self.customer.trace_add("write", lambda *_: self.debounced_select(source="customer"))
        else:
            self.customer.trace_remove("write", self.text_trace)
            self.text_trace = self.customer.trace_add("write", self.show_suggestions)
        self.on_select()


    def debounced_select(self, *args, source=None):
        if self.search_job is not None:
            self.after_cancel(self.search_job)
        self.search_job = self.after(300, lambda: self.on_select(*args, source=source))


    def close_listbox(self, *_):
        if self.listbox:
            self.listbox.destroy()
            self.listbox = None


    def on_select(self, *_, source=None):
        if not self.root.all_companies.get():
            if self.listbox: 
                selection = self.listbox.selection()
                if selection:
                    self.customer.set(self.listbox.item(selection[0], "values")[0])
                else:
                    items = self.listbox.get_children()
                    if len(items) == 1:
                        self.customer.set(self.listbox.item(items[0], "values")[0])

            customer = self.customer.get()
            if self.root.search_names.get():
                customer_l = customer.lower()
                valid = any(tup[0].lower() == customer_l or customer_l in tup[1].lower() for tup in self.customer_ids)
            else:
                valid = any(customer in tup for tup in self.customer_ids)
            if not valid:
                self.tree.delete(*self.tree.get_children())
                return
        else:
            customer = self.customer.get()
            
        if self.root.ignoring and customer in self.root.ignore_list:
            self.tree.delete(*self.tree.get_children())
            return
            
        invoice_prefix = self.root.invoice_text.get()

        narrow = False
        if ((source == "customer" and customer.startswith(self.prev_customer) and not customer == self.prev_customer) or
            (source == "invoice" and invoice_prefix.startswith(self.root.prev_invoice_text) and not invoice_prefix == self.root.prev_invoice_text)):
            narrow = True
            
        self.prev_customer = customer
        self.root.prev_invoice_text = invoice_prefix

        self.root.config(cursor="watch")
        self.config(cursor="watch")
        self.root.tree.config(cursor="watch")
        self.root.amount_label.config(text="Loading...")
        self.after(25, lambda: self.search(customer, invoice_prefix, narrow))

    
    def search(self, customer, invoice_prefix, narrow):
        if narrow and self.root.current_rows:
            invoice_count = self.root.filter_rows(customer, invoice_prefix)
        else:
            invoice_count, values = self.root.show_invoices(customer, invoice_prefix)
            self.root.sort_by(self.root.sort_col, values, header_pressed=False, watch_cursor=False)

        self.root.result_count = invoice_count
        self.root.update_result_label()
        self.root.config(cursor="")
        self.config(cursor="")
        self.root.tree.config(cursor="")
        self.close_listbox()

    def listbox_move(self, dir):
        if not self.listbox:
            return
        
        dir = 1 if dir == "down" else -1
        
        rows = self.listbox.get_children()
        if not rows:
            return

        current = self.listbox.selection()
        i = rows.index(current[0]) if current else -1
        i = (i + dir) % len(rows)
        self.listbox.selection_set(rows[i])
        self.listbox.focus(rows[i])
        self.listbox.see(rows[i])


    def show_cell_menu(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return

        row = self.tree.identify_row(event.y)
        if not row:
            return

        col_id = self.tree.identify_column(event.x)
        if col_id == "#0":
            return

        col_index = int(col_id[1:]) - 1
        columns = self.tree["columns"]
        if col_index < 0 or col_index >= len(columns):
            return
        col_name = columns[col_index]

        selection = self.tree.selection()
        if row in selection and len(selection) > 1:
            rows = self.ordered_selection()
        else:
            self.tree.selection_set(row)
            self.tree.focus(row)
            rows = [row]

        heading = self.tree.heading(col_name)["text"].replace("  ▼", "").replace("  ▲", "").strip()

        menu = tk.Menu(self.tree, tearoff=0)
        if len(rows) > 1:
            n = len(rows)
            menu.add_command(label=f"Copy {heading} ({n} rows)", command=lambda: self.copy_column(rows, col_name))
            menu.add_command(label=f"Copy Rows ({n} rows)", command=lambda: self.copy_rows(rows))
        else:
            value = self.tree.set(row, col_name)
            if value and value not in ("▼", "▲", "✔"):
                menu.add_command(label=f"Copy {heading}", command=lambda: self.copy_to_clipboard(value))
            else:
                menu.add_command(label=f"Copy {heading}", state="disabled")
            menu.add_command(label="Copy Row", command=lambda: self.copy_row(row))
            menu.add_command(label="Copy Date & Invoice", command=lambda: self.copy_date_invoice(row))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()


    def ordered_selection(self):
        selected = set(self.tree.selection())
        ordered = []
        def walk(parent):
            for child in self.tree.get_children(parent):
                if child in selected:
                    ordered.append(child)
                walk(child)
        walk("")
        return ordered


    def copy_to_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)


    def row_text(self, row):
        cells = []
        for col in self.tree["columns"]:
            if col == "Filepath":
                continue
            val = self.tree.set(row, col)
            cells.append(val)
        return "\t".join(cells)


    def copy_row(self, row):
        self.copy_to_clipboard(self.row_text(row))


    def copy_date_invoice(self, row):
        date = self.tree.set(row, "Date")
        invoice = self.tree.set(row, "Invoice")
        date_formatted = datetime.strptime(date, "%Y-%m-%d").strftime("%m-%d-%y")
        self.copy_to_clipboard(f"{date_formatted}_{invoice}")


    def copy_column(self, rows, col_name):
        lines = []
        for row in rows:
            val = self.tree.set(row, col_name)
            lines.append(val)
        self.copy_to_clipboard("\n".join(lines))


    def copy_rows(self, rows):
        self.copy_to_clipboard("\n".join(self.row_text(row) for row in rows))


class ErrorPopup(tk.Toplevel):
    def __init__(self, root, terrors, ierrors, missing:list[tuple], duplicates:list[str], **kw):
        super().__init__(root, **kw)
        self.root = root
        self.title("Error Page")
        self.wm_attributes("-toolwindow", True)
        self.withdraw()
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.toggle)
        self.bind("<space>", self.toggle)
        self.geometry("750x400")

        frame = tk.Frame(self)
        frame.pack(expand=True, fill="both")

        self.text = scrolledtext.ScrolledText(frame, font=font.Font(family="Consolas", size=9), wrap="none")
        self.text.pack(expand=True, fill="both")
        hscroll = tk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        hscroll.pack(side="bottom", fill="x")
        self.text.configure(xscrollcommand=hscroll.set)
        self.text.tag_configure("bold", font=font.Font(family="Consolas", size=12, weight="bold"))
        
        terrors.sort(key=lambda x: x["CustomerID"])
        self.text.insert(tk.END, f" {len(terrors)} Titan AR Invoice Errors\n", ("bold",))
        for row in terrors:
            self.text.insert(tk.END, f" -{row}\n")

        self.text.insert(tk.END, f"\n {len(ierrors)} Invoice File Errors\n", ("bold",))
        for row in ierrors:
            self.text.insert(tk.END, f" -{row}\n")

        if duplicates:
            self.text.insert(tk.END, f"\n {len(duplicates)} Duplicate Invoices Found in Database\n", ("bold",))
            for row in duplicates:
                self.text.insert(tk.END, f" -{row}\n")

        missing.sort(key=lambda x: (x[0], -datetime.strptime(x[2], "%m-%d-%Y").timestamp()))
        self.text.insert(tk.END, f"\n {len(missing)} Invoices Missing Files\n", ("bold",))
        for row in missing:
            self.text.insert(tk.END, f" -{row}\n")

    
    def toggle(self, *_):
        if self.state() == "withdrawn":
            self.show()
        else:
            self.withdraw()


    def show(self):
        ax, ay = self.root.winfo_rootx(), self.root.winfo_rooty()
        aw, ah = self.root.winfo_width(), self.root.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        x = ax + (aw - w) // 2
        y = ay + (ah - h) // 3
        self.geometry(f"+{x}+{y}")
        self.deiconify()


class HelpPopup(tk.Toplevel):
    def __init__(self, root, **kw):
        super().__init__(root, **kw)
        self.root = root
        self.title("Help Page")
        self.wm_attributes("-toolwindow", True)
        self.withdraw()
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.toggle)
        self.bind("<space>", self.toggle)
        self.geometry("700x600")

        frame = tk.Frame(self)
        frame.pack(expand=True, fill="both")
        
        self.text = scrolledtext.ScrolledText(frame, font=font.Font(family="Consolas", size=9), wrap="none")
        self.text.pack(expand=True, fill="both")

        self.text.tag_configure("header", font=font.Font(family="Consolas", size=10, weight="bold"), spacing1=8)
        self.text.tag_configure("title",  font=font.Font(family="Consolas", size=12, weight="bold"), spacing1=4)
        self.text.tag_configure("body",   font=font.Font(family="Consolas", size=9), lmargin1=10, lmargin2=10)
        self.text.tag_configure("indent", font=font.Font(family="Consolas", size=9), lmargin1=26, lmargin2=26)
        self.text.tag_configure("footer", font=font.Font(family="Consolas", size=9), spacing1=10)

        def h(text): self.text.insert(tk.END, text + "\n", "header")
        def b(text): self.text.insert(tk.END, text + "\n", "body")
        def i(text): self.text.insert(tk.END, text + "\n", "indent")
        def f(text): self.text.insert(tk.END, text + "\n", "footer")

        self.text.insert(tk.END, "Titan AR Invoice Viewer — Help\n", "title")

        h("GETTING STARTED")
        b("When the program opens it loads all AR Invoice data from the Titan database and scans")
        b("the invoice file directory. This takes a few seconds. Once loading is complete the")
        b("main invoice table and search bar will appear")
        b("• Press the ⭮ Refresh button (top-right) at any time to reload the latest data")
        b("• Press the Errors button to see any invoices or files that had problems loading")

        h("SEARCHING FOR INVOICES")
        b("Customer ID  — Type a customer ID into the Customer ID box. A suggestion list will")
        b("appear; click a result or press Enter to load that customer's invoices")
        b("")
        b("View All Customers  — Check this box to show invoices across all customers at once")
        b("In this mode the Customer ID box becomes a prefix filter: typing 'AC' shows every")
        b("customer whose ID starts with 'AC', rather than requiring an exact match")
        b("")
        b("Search Names  — Check this box to also match customer names, not just IDs. With it on,")
        b("typing part of a name (e.g. 'concrete') finds every customer whose name contains that")
        b("text, and the suggestion list shows those matches too. Works alongside the options")
        b("above. Leave it off to search by customer ID only")
        b("")
        b("Invoice  — Type in the Invoice box to narrow results to invoices whose number")
        b("starts with your entry")
        b("Search Invoice Names  — Check this box to match your entry anywhere in the invoice")
        b("number instead of only at the start. Typing '123' finds invoice 4123B as well as 1234")
        b("")
        b("Start / End Date  — Only invoices within this date range will be shown")
        b("")
        b("File Available Only  — Check this box to hide any invoices that do not have a")
        b("PDF file stored on the network drive")

        h("THE INVOICE TABLE")
        b("Each row is one invoice. The columns show:")
        i("Customer        — The customer ID code")
        i("Company Name    — The customer's full company name")
        i("Invoice         — The invoice number")
        i("Date            — The invoice date")
        i("State           — The state for the invoice (from County)")
        i("Subtotal        — The pre-tax and pre-discount total")
        i("Tax             — The total tax amount")
        i("Discount        — Early pay or standard discounts applied")
        i("Total           — The final post-tax and discount total")
        i("Payments        — The total amount paid by the customer to date")
        i("Closed          — Indicates 'Yes' if the invoice is fully paid and closed")
        i("File Available  — A ✔ means a PDF of the invoice is stored on the network")

        h("OPENING INVOICE FILES")
        b("Double left-click any row that has a ✔ in the File Available column to open the")
        b("invoice PDF")

        h("COPYING DATA")
        b("Right-click any cell to open a small menu")
        i("Copy <Column>   — Copies just that cell, e.g. an invoice number")
        i("Copy Row — Copies the whole row, tab-separated (pastes neatly into Excel)")
        i("Copy Date & Invoice — Copies the date and invoice number for one row, underscore-separated")
        b("")
        b("To copy several rows at once, select them first (Ctrl-click or Shift-click), then")
        b("right-click any cell within the selection. The menu changes to:")
        i("Copy <Column> (N rows)    — Copies that one column's value from every selected row,")
        i("                            one per line")
        i("Copy Rows (N rows) — Copies every selected row in full, one row per line")
        b("Both multi-row options paste straight into Excel as rows and columns")

        h("SORTING")
        b("Click any column header to sort the table by that column. Click the same header")
        b("again to reverse the sort direction. The active sort column is marked with ▲ or ▼")

        h("SELECTING ROWS AND TOTALS")
        b("Click a row to select it. The totals bar at the bottom of the window shows:")
        i("Selected Total — Sum of the 'Total' column for only the rows you have selected")
        i("Invoice Total  — Sum of the 'Total' column for all visible invoices")
        i("Payments Total — Sum of all payments received for all visible invoices")
        i("AR Total       — The remaining balance (Invoice Total minus Payments Total)")
        b("To select multiple rows hold Ctrl and click individual rows, or hold Shift and")
        b("click to select a continuous range")

        f("Questions or suggestions can be sent to jmwesthoff@atlanticconcrete.com")


    def toggle(self, *_):
        if self.state() == "withdrawn":
            self.show()
        else:
            self.withdraw()


    def show(self):
        ax, ay = self.root.winfo_rootx(), self.root.winfo_rooty()
        aw, ah = self.root.winfo_width(), self.root.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        x = ax + (aw - w) // 2
        y = ay + (ah - h) // 3
        self.geometry(f"+{x}+{y}")
        self.deiconify()
    

if __name__ == "__main__":
    invoice_viewer = InvoiceViewer()
    invoice_viewer.mainloop()