import os
import glob
import json
import shutil
import csv
import re
import warnings
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

import torch
import torch.nn as nn
import torchvision.transforms as T
import timm
from torch.utils.data import DataLoader, Dataset
import pandas as pd
import numpy as np

#==========================================
#CONFIGURATION
#==========================================

#1. PATHS
BASE_DIR = r"C:\Users\austi\Documents\TomCat VI Training\ClassifierModelTraining\ClassifierTrainingData\sortedPics\HITL_Crops"

DIR_AMBIGUOUS = os.path.join(BASE_DIR, "ambiguous_or_groups")
DIR_KNOWN     = os.path.join(BASE_DIR, "known_cats")
DIR_TARGET    = os.path.join(BASE_DIR, "HITL_labeled_crops")
DIR_REJECT    = os.path.join(BASE_DIR, "HITL_rejects")

CSV_PATH      = os.path.join(BASE_DIR, "Catabase - TCB Pics Formatted.csv")

#ReIDencoder+gallery (new format)
ENCODER_WEIGHTS = r"C:\Users\austi\Documents\TomCat VI Training\Weights\DINOv3 Classifier\R4_cat_DINOv3_encoder.pth"
GALLERY_PATH    = r"C:\Users\austi\Documents\TomCat VI Training\Weights\DINOv3 Classifier\R4.5_cat_DINOv3_gallery.pt"

#Gallery speed knobs
MAX_REFS_PER_CAT = 200#NoSpace:keep scoring fast
GALLERY_SEED = 1337#NoSpace:stable sampling when a cat has lots of refs

#2. MODEL CONFIG
MODEL_NAME = "vit_base_patch16_dinov3"
IMG_SIZE = 448
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

#3. BAYESIAN SHRINKAGE CONFIG
#GLOBAL_MEAN_DIST: skeptical baseline distance, cats with few samples get pulled toward this (0.7 = probably not confident)
GLOBAL_MEAN_DIST = 0.7
#SHRINKAGE_STRENGTH: samples needed to trust observed distance (at N=8, blend 50/50 between observed and global mean)
SHRINKAGE_STRENGTH = 8
#POPULARITY_BONUS: per-reference-photo distance reduction (max 0.05 total). Favors cats with more samples.
POPULARITY_BONUS_PER_REF = 0.002
POPULARITY_BONUS_MAX = 0.05

#==========================================
#CORE MODEL & UTILS
#==========================================

class ReIDModel(nn.Module):
    def __init__(self, model_name, emb_dim=512):
        super().__init__()
        self.backbone = timm.create_model(model_name, pretrained=False, num_classes=0)
        in_features = self.backbone.num_features
        self.head = nn.Sequential(
            nn.Linear(in_features, emb_dim),
            nn.BatchNorm1d(emb_dim),
            nn.PReLU()
        )

    def forward(self, x):
        f = self.backbone(x)
        e = self.head(f)
        return nn.functional.normalize(e, p=2, dim=1)

def get_transforms():
    mean = [0.485, 0.456, 0.406]
    std  = [0.229, 0.224, 0.225]
    
    def letterbox(img):
        w, h = img.size
        scale = IMG_SIZE / max(w, h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        img = img.resize((nw, nh), Image.BICUBIC)
        new_img = Image.new("RGB", (IMG_SIZE, IMG_SIZE), (124, 116, 104))
        new_img.paste(img, ((IMG_SIZE - nw)//2, (IMG_SIZE - nh)//2))
        return new_img

    return T.Compose([
        T.Lambda(letterbox),
        T.ToTensor(),
        T.Normalize(mean, std)
    ])

#==========================================
#LOGIC ENGINE
#==========================================

#==========================================
#DARK THEME COLORS
#==========================================
COLOR_BG_DARK = "#1a1a1a"      #Main background
COLOR_BG_PANEL = "#252525"     #Panel backgrounds
COLOR_BG_CARD = "#2d2d2d"      #Card/item backgrounds
COLOR_BORDER = "#3d3d3d"       #Border color
COLOR_TEXT = "#e0e0e0"         #Primary text
COLOR_TEXT_DIM = "#888888"     #Secondary text
COLOR_ACCENT = "#4a9eff"       #Accent color
COLOR_ACCENT_WARN = "#ff9f43"  #Warning/global search

class CatSorterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("TomCat Vision: HITL Sorter")
        self.root.geometry("1400x800")  #Smaller starting size
        self.root.configure(bg=COLOR_BG_DARK)
        
        #--- State ---
        self.transform = get_transforms()

        self.reference_gallery = {}
        self.gallery_emb_dim = None
        self._gallery_check_path = None
        self._gallery_check_emb = None

        #Loadgallery first so we know embedding dim before creating the model
        self.build_initial_gallery()
        self.model = self.load_model(self.gallery_emb_dim)

        self.sn_map = {}
        self.current_sn = None
        self.used_labels_in_sn = set()
        
        #--- History for Undo ---
        self.action_history = []  #List of dicts: {'src': file_path, 'dst': destination_path, 'label': cat_name or None}
        
        #--- Progress Tracking ---
        self.total_initial_count = 0
        self.completed_count = 0
        
        #--- UI Components ---
        self.setup_ui()
        
        #--- Initialization ---
        self.status("Loading Database...")
        self.root.update()
        
        self.parse_csv()
        self.build_initial_gallery()
        self.scan_ambiguous_folder()
        
        self.status("Ready.")
        self.next_image()

    def load_model(self, emb_dim):
        print(f"Loading encoder from {ENCODER_WEIGHTS}...")

        if emb_dim is None:
            raise RuntimeError("Gallery embedding dim is unknown. Did gallery load fail?")

        #Suppress torch.load FutureWarning noise
        warnings.filterwarnings("ignore", category=FutureWarning, module="torch")

        model = ReIDModel(MODEL_NAME, emb_dim=emb_dim).to(DEVICE)

        try:
            #This file is a plain state_dict (OrderedDict)
            state = torch.load(ENCODER_WEIGHTS, map_location="cpu", weights_only=True)
            missing, unexpected = model.load_state_dict(state, strict=False)

            if missing:
                print("WARNING:Missing keys:", missing[:20], "..." if len(missing) > 20 else "")
            if unexpected:
                print("WARNING:Unexpected keys:", unexpected[:20], "..." if len(unexpected) > 20 else "")

        except Exception as e:
            messagebox.showerror("Error", f"Could not load encoder weights:\n{e}")
            raise

        model.eval()

        #Hard check 1:dim match
        try:
            with torch.no_grad():
                dummy = torch.zeros(1, 3, IMG_SIZE, IMG_SIZE, device=DEVICE)
                out = model(dummy)
            if out.shape[1] != emb_dim:
                raise RuntimeError(f"Model output dim {out.shape[1]} != gallery dim {emb_dim}")
        except Exception as e:
            messagebox.showerror("Error", f"Model/gallery dimension check failed:\n{e}")
            raise

        #Hard check 2:does this encoder reproduce at least one gallery embedding?
        if self._gallery_check_path and os.path.exists(self._gallery_check_path) and self._gallery_check_emb is not None:
            try:
                img = Image.open(self._gallery_check_path).convert("RGB")
                t_img = self.transform(img).unsqueeze(0).to(DEVICE)
                with torch.no_grad():
                    e = model(t_img).cpu()#(1,D)
                sim = torch.nn.functional.cosine_similarity(e, self._gallery_check_emb, dim=1).item()
                print(f"Gallery consistency check sim={sim:.4f} on {os.path.basename(self._gallery_check_path)}")
                if sim < 0.90:
                    raise RuntimeError(
                        "Encoder does not match gallery embeddings. "
                        "Likely IMG_SIZE/letterbox or model variant mismatch, or wrong weights paired with gallery."
                    )
            except Exception as e:
                messagebox.showerror("Error", f"Encoder/gallery consistency check failed:\n{e}")
                raise

        return model

    def parse_csv(self):
        print("Parsing CSV...")
        if not os.path.exists(CSV_PATH):
            messagebox.showwarning("Warning", "CSV not found. Logic selection won't work.")
            return

        pattern = re.compile(r'\d+\.\s*([^,]+)')

        try:
            df = pd.read_csv(CSV_PATH)
            
            #--- Column Detection ---
            #Clean whitespaces from column names
            df.columns = [c.strip() for c in df.columns]
            
            #Find 'Serial number' column (case insensitive)
            sn_col_name = None
            for col in df.columns:
                if "serial" in col.lower():
                    sn_col_name = col
                    break
            
            #Fallback: try index 7 (Column H)
            if not sn_col_name:
                print("Could not find 'Serial number' column by name. Trying index 7...")
                if len(df.columns) > 7:
                    sn_col_name = df.columns[7]
            
            if not sn_col_name:
                print("CRITICAL: Could not identify Serial Number column in CSV.")
                return

            print(f"Using column '{sn_col_name}' for Serial Numbers.")

            for index, row in df.iterrows():
                #Column 0 is always CatID info
                raw_names = str(row.iloc[0]) 
                raw_sn = str(row[sn_col_name])
                
                #Clean SN: remove .jpg, handle float->int (9.0 -> "9")
                if raw_sn.endswith(".0"):
                    raw_sn = raw_sn[:-2]
                    
                sn = raw_sn.replace(".jpg", "").strip()
                
                if sn == "nan" or not sn: continue
                
                matches = pattern.findall(raw_names)
                cleaned_names = [m.strip() for m in matches]
                
                if cleaned_names:
                    self.sn_map[sn] = cleaned_names
                    
            print(f"Loaded {len(self.sn_map)} serial number mappings.")
            
        except Exception as e:
            print(f"CSV Parsing Error: {e}")

    def get_embedding(self, img_path):
        try:
            img = Image.open(img_path).convert("RGB")
            t_img = self.transform(img).unsqueeze(0).to(DEVICE)
            with torch.no_grad():
                emb = self.model(t_img)
            return emb.cpu()
        except Exception as e:
            print(f"Error processing {img_path}: {e}")
            return None

    def build_initial_gallery(self):
        print("Loading reference gallery from file...")

        if not os.path.exists(GALLERY_PATH):
            messagebox.showerror("Error", f"Gallery file not found:\n{GALLERY_PATH}")
            raise FileNotFoundError(GALLERY_PATH)

        warnings.filterwarnings("ignore", category=FutureWarning, module="torch")

        try:
            #Gallery contains tensors + lists + dict, so weights_only=True may or may not work depending on torch version
            try:
                g = torch.load(GALLERY_PATH, map_location="cpu", weights_only=True)
            except Exception:
                g = torch.load(GALLERY_PATH, map_location="cpu", weights_only=False)

            emb = g["emb"]
            lab = g["label"]
            paths = g["path"]
            class_to_idx = g["class_to_idx"]
        except Exception as e:
            messagebox.showerror("Error", f"Could not load gallery:\n{e}")
            raise

        if not torch.is_tensor(emb):
            emb = torch.tensor(emb)
        if not torch.is_tensor(lab):
            lab = torch.tensor(lab)

        if emb.ndim != 2:
            raise RuntimeError(f"Gallery emb must be 2D (N,D). Got shape {tuple(emb.shape)}")
        if lab.ndim != 1:
            lab = lab.view(-1)
        if emb.shape[0] != lab.shape[0] or emb.shape[0] != len(paths):
            raise RuntimeError(f"Gallery size mismatch: embN={emb.shape[0]} labelN={lab.shape[0]} pathN={len(paths)}")

        #Normalize to be safe
        emb = torch.nn.functional.normalize(emb.float(), p=2, dim=1)

        self.gallery_emb_dim = int(emb.shape[1])
        self.reference_gallery = {}

        idx_to_class = {v: k for k, v in class_to_idx.items()}

        #Group indices by cat
        by_cat = {}
        for i in range(emb.shape[0]):
            ci = int(lab[i].item())
            cat = idx_to_class.get(ci, str(ci))
            by_cat.setdefault(cat, []).append(i)

        rnd = random.Random(GALLERY_SEED)

        #Pick a stable subset per cat (keeps scoring fast)
        self._gallery_check_path = None
        self._gallery_check_emb = None

        total_loaded = 0
        for cat, idxs in sorted(by_cat.items(), key=lambda x: x[0].lower()):
            if len(idxs) > MAX_REFS_PER_CAT:
                idxs = rnd.sample(idxs, MAX_REFS_PER_CAT)

            refs = []
            for i in idxs:
                p = paths[i]
                refs.append({
                    "emb": emb[i:i+1],#(1,D)
                    "path": p
                })

                #Save one example for encoder/gallery sanity check later
                if self._gallery_check_path is None and isinstance(p, str) and os.path.exists(p):
                    self._gallery_check_path = p
                    self._gallery_check_emb = emb[i:i+1]

            self.reference_gallery[cat] = refs
            total_loaded += len(refs)

        print(f"Gallery loaded:{len(self.reference_gallery)} cats, {total_loaded} refs, emb_dim={self.gallery_emb_dim}")
    


    def scan_ambiguous_folder(self):
        self.pending_files = sorted(glob.glob(os.path.join(DIR_AMBIGUOUS, "*.*")))
        
        #Count total images across ALL folders for accurate progress
        n_ambiguous = len(self.pending_files)
        
        #Count rejected images
        n_rejected = 0
        if os.path.exists(DIR_REJECT):
            n_rejected = len([f for f in os.listdir(DIR_REJECT) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
        
        #Count labeled images (files in all subdirectories)
        n_labeled = 0
        if os.path.exists(DIR_TARGET):
            for cat_dir in os.listdir(DIR_TARGET):
                cat_path = os.path.join(DIR_TARGET, cat_dir)
                if os.path.isdir(cat_path):
                    n_labeled += len([f for f in os.listdir(cat_path) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
        
        self.total_initial_count = n_ambiguous + n_rejected + n_labeled
        self.completed_count = n_rejected + n_labeled  #Already processed
        
        print(f"Progress: {self.completed_count}/{self.total_initial_count} total images.")
        print(f"  - Pending: {n_ambiguous}")
        print(f"  - Rejected: {n_rejected}")
        print(f"  - Labeled: {n_labeled}")

    #==========================================
    #MAIN LOOP
    #==========================================

    def next_image(self):
        if not self.pending_files:
            messagebox.showinfo("Done", "No more images in ambiguous folder!")
            self.root.quit()
            return

        self.current_file_path = self.pending_files.pop(0)
        filename = os.path.basename(self.current_file_path)
        
        #Parse Serial Number: sn0424_crop0.jpg -> 424
        match = re.search(r'sn(\d+)', filename)
        if match:
            sn_str = match.group(1)
            #Remove leading zeros to match CSV (0009 -> 9)
            sn_int_str = str(int(sn_str))
        else:
            sn_str = "UNKNOWN"
            sn_int_str = "UNKNOWN"

        #Manage session state for the SN
        if self.current_sn != sn_str:
            self.current_sn = sn_str
            self.used_labels_in_sn = set() 

        #Lookup: try "9", then try "0009"
        possible_cats = self.sn_map.get(sn_int_str, [])
        if not possible_cats:
            possible_cats = self.sn_map.get(sn_str, [])
        
        #Filter used labels
        active_candidates = [c for c in possible_cats if c not in self.used_labels_in_sn]

        self.current_embedding = self.get_embedding(self.current_file_path)
        if self.current_embedding is None:
            self.next_image() 
            return

        def score_cat(cat_name):
            """Score a single cat and return (name, distance, ref_paths) tuple."""
            refs = self.reference_gallery.get(cat_name, [])
            
            if not refs: 
                return (cat_name, 9999, [])
                
            dists = []
            for ref in refs:
                sim = torch.nn.functional.cosine_similarity(self.current_embedding, ref['emb'])
                dists.append(1 - sim.item())
            
            raw_avg = sum(dists) / len(dists)
            n = len(refs)
            shrinkage_factor = n / (n + SHRINKAGE_STRENGTH)
            avg_dist = shrinkage_factor * raw_avg + (1 - shrinkage_factor) * GLOBAL_MEAN_DIST
            
            #Popularity bonus: cats with more refs get slight distance reduction
            popularity_bonus = min(n * POPULARITY_BONUS_PER_REF, POPULARITY_BONUS_MAX)
            avg_dist = avg_dist - popularity_bonus
            
            #Get 4 most similar reference photos
            ref_with_dist = []
            for ref in refs:
                sim = torch.nn.functional.cosine_similarity(self.current_embedding, ref['emb'])
                ref_with_dist.append((ref['path'], 1 - sim.item()))
            ref_with_dist.sort(key=lambda x: x[1])
            ref_paths = [r[0] for r in ref_with_dist[:4]]
            
            return (cat_name, avg_dist, ref_paths)

        #--- HYBRID APPROACH: Always show 9 total options ---
        #Section 1: Metadata candidates (up to 5)
        metadata_scored = []
        if active_candidates:
            for cat in active_candidates:
                metadata_scored.append(score_cat(cat))
            metadata_scored.sort(key=lambda x: x[1])
            metadata_scored = metadata_scored[:5]
        
        #Section 2: Global guesses - fill remaining slots to reach 9 total
        num_metadata = len(metadata_scored)
        num_global_needed = 9 - num_metadata
        
        metadata_cat_names = set(c[0] for c in metadata_scored)
        global_candidates = [cat for cat in self.reference_gallery.keys() 
                            if cat not in metadata_cat_names and cat not in self.used_labels_in_sn]
        
        global_scored = []
        for cat in global_candidates:
            global_scored.append(score_cat(cat))
        global_scored.sort(key=lambda x: x[1])
        global_scored = global_scored[:num_global_needed]
        
        #Combine with section markers: ("__SECTION__", section_name, None)
        self.current_candidates = []
        
        if metadata_scored:
            self.current_candidates.append(("__SECTION__", "Metadata Labels", None))
            self.current_candidates.extend(metadata_scored)
        
        if global_scored:
            self.current_candidates.append(("__SECTION__", "Global Guesses", None))
            self.current_candidates.extend(global_scored)
        
        #Pure global search (no metadata) - no section header needed
        if not metadata_scored and global_scored:
            self.current_candidates = global_scored

        is_global_search = not metadata_scored
        self.update_display(filename, sn_str, is_global=is_global_search)

    #==========================================
    #UI RENDERING
    #==========================================

    def setup_ui(self):
        #--- Left Panel (Mystery Image) ---
        self.left_panel = tk.Frame(self.root, width=600, bg=COLOR_BG_PANEL)
        self.left_panel.pack(side=tk.LEFT, fill=tk.Y, padx=15, pady=15)
        
        self.lbl_mystery = tk.Label(self.left_panel, bg=COLOR_BG_PANEL, borderwidth=2, relief="groove")
        self.lbl_mystery.pack(expand=True, pady=(20, 10))
        
        self.lbl_filename = tk.Label(
            self.left_panel, 
            text="Filename", 
            fg=COLOR_TEXT, 
            bg=COLOR_BG_PANEL, 
            font=("Segoe UI", 13),
            justify="center"
        )
        self.lbl_filename.pack(pady=10)
        
        #--- Instructions Label ---
        instructions = "Keys: 1-9 = Select  |  0 = Reject  |  R = Global Only  |  Backspace = Undo"
        self.lbl_instructions = tk.Label(
            self.left_panel,
            text=instructions,
            fg=COLOR_TEXT_DIM,
            bg=COLOR_BG_PANEL,
            font=("Segoe UI", 10)
        )
        self.lbl_instructions.pack(pady=(5, 20))

        #--- Right Panel (Candidates) ---
        self.right_panel = tk.Frame(self.root, bg=COLOR_BG_DARK)
        self.right_panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=15, pady=15)
        
        self.candidate_widgets = []
        for i in range(12):  #9 candidates + up to 2 section headers
            fr = tk.Frame(
                self.right_panel, 
                bg=COLOR_BG_CARD, 
                borderwidth=1, 
                relief="flat",
                highlightbackground=COLOR_BORDER,
                highlightthickness=1
            )
            self.candidate_widgets.append(fr)
        
        self.root.bind("<Key>", self.handle_keypress)
        
        #Bind resize event for responsive UI
        self.root.bind("<Configure>", self.on_resize)
        self._last_resize_size = (0, 0)

    def on_resize(self, event):
        """Handle window resize events for responsive UI."""
        #Only respond to root window resize, not child widgets
        if event.widget != self.root:
            return
        
        new_size = (event.width, event.height)
        #Debounce: only redraw if size changed significantly
        if abs(new_size[0] - self._last_resize_size[0]) > 20 or abs(new_size[1] - self._last_resize_size[1]) > 20:
            self._last_resize_size = new_size
            #Re-render display if we have an image loaded
            if self.current_file_path and os.path.exists(self.current_file_path):
                #Parse SN for display
                filename = os.path.basename(self.current_file_path)
                match = re.search(r'sn(\d+)', filename)
                sn_str = match.group(1) if match else "UNKNOWN"
                is_global = len(self.current_candidates) > 0 and self.current_candidates[0][1] == 9999
                self.update_display(filename, sn_str, is_global=is_global)

    def update_display(self, filename, sn, is_global=False):
        #Get current window dimensions for responsive sizing
        win_width = self.root.winfo_width()
        win_height = self.root.winfo_height()
        
        #Calculate mystery image size based on window (left panel ~40% width)
        left_panel_width = max(400, int(win_width * 0.35))
        mystery_size = min(left_panel_width - 50, win_height - 200)
        mystery_size = max(300, mystery_size)  #Minimum size
        
        img = Image.open(self.current_file_path)
        img.thumbnail((mystery_size, mystery_size))
        photo = ImageTk.PhotoImage(img)
        self.lbl_mystery.config(image=photo)
        self.lbl_mystery.image = photo 
        
        mode_text = "GLOBAL SEARCH" if is_global else "FILTERED"
        color = COLOR_ACCENT_WARN if is_global else COLOR_TEXT
        
        #Progress indicator: current position / total
        current_pos = self.completed_count + 1
        total = self.total_initial_count
        progress_text = f"{current_pos} / {total}"
        
        self.lbl_filename.config(
            text=f"{filename}\nSN: {sn}\n\n{progress_text}\n[{mode_text}]", 
            fg=color
        )

        #Clear previous candidate widgets
        for w in self.candidate_widgets:
            for child in w.winfo_children():
                child.destroy()
            w.pack_forget()

        #Dynamic sizing based on window height and number of actual candidates (not headers)
        actual_candidates = [c for c in self.current_candidates if c[0] != "__SECTION__"]
        num_actual = len(actual_candidates)
        if num_actual == 0:
            return
        
        #Calculate available height for candidates (account for section headers)
        num_sections = len([c for c in self.current_candidates if c[0] == "__SECTION__"])
        available_height = win_height - 60 - (num_sections * 25)
        row_height = max(60, available_height // max(num_actual, 1))
        
        #Calculate thumbnail size - use MAXIMUM of available dimensions
        right_panel_width = win_width - left_panel_width - 40
        label_space = 220  #Space for cat name and distance
        available_thumb_width = right_panel_width - label_space
        
        #Size based on fitting 4 thumbnails with spacing
        thumb_from_width = (available_thumb_width // 4) - 8
        thumb_from_height = row_height - 16
        
        #Use the larger dimension when there's clearly space to grow
        thumb_size = max(70, max(thumb_from_width, thumb_from_height))
        #Cap at row height to prevent overlap
        thumb_size = min(thumb_size, row_height - 8)
        
        row_pady = max(1, (row_height - thumb_size) // 4)

        key_idx = 0
        widget_idx = 0
        
        for entry in self.current_candidates:
            if widget_idx >= 12: break
            
            cat_name, dist, ref_paths = entry
            
            #--- SECTION HEADER ---
            if cat_name == "__SECTION__":
                section_name = dist
                frame = self.candidate_widgets[widget_idx]
                frame.pack(fill=tk.X, pady=(6, 1), padx=5)
                widget_idx += 1
                
                separator = tk.Frame(frame, bg="#555555", height=1)
                separator.pack(fill=tk.X, pady=(0, 2))
                
                lbl_section = tk.Label(
                    frame,
                    text=section_name,
                    font=("Segoe UI", 9, "italic"),
                    fg=COLOR_TEXT_DIM,
                    bg=COLOR_BG_CARD,
                    anchor="w"
                )
                lbl_section.pack(side=tk.LEFT, padx=(10, 0))
                continue
            
            #--- REGULAR CANDIDATE ---
            if key_idx >= 9: break
            key_idx += 1
            
            frame = self.candidate_widgets[widget_idx]
            frame.pack(fill=tk.X, pady=row_pady, padx=5)
            widget_idx += 1
            
            if dist < 0.5:
                dist_color = "#4ade80"
            elif dist < 0.7:
                dist_color = "#fbbf24"
            else:
                dist_color = "#f87171"
            
            header_txt = f"  {key_idx}.  {cat_name}"
            lbl_head = tk.Label(
                frame, 
                text=header_txt, 
                font=("Segoe UI", 12, "bold"), 
                anchor="w",
                fg=COLOR_TEXT,
                bg=COLOR_BG_CARD,
                width=20
            )
            lbl_head.pack(side=tk.LEFT, padx=(10, 5))
            
            lbl_dist = tk.Label(
                frame,
                text=f"[{dist:.3f}]",
                font=("Segoe UI", 10),
                fg=dist_color,
                bg=COLOR_BG_CARD
            )
            lbl_dist.pack(side=tk.LEFT, padx=(0, 10))
            
            #Reference thumbnails with filename overlay
            for r_path in ref_paths:
                try:
                    r_img = Image.open(r_path)
                    r_img.thumbnail((thumb_size, thumb_size))
                    r_photo = ImageTk.PhotoImage(r_img)
                    
                    #Container for image + filename overlay
                    thumb_container = tk.Frame(frame, bg=COLOR_BG_CARD)
                    thumb_container.pack(side=tk.LEFT, padx=2)
                    
                    lbl_thumb = tk.Label(
                        thumb_container, 
                        image=r_photo, 
                        bg=COLOR_BG_CARD,
                        borderwidth=1,
                        relief="solid"
                    )
                    lbl_thumb.image = r_photo
                    lbl_thumb.pack()
                    
                    #Filename overlay (just the basename, truncated)
                    fname = os.path.basename(r_path)
                    if len(fname) > 18:
                        fname = fname[:15] + "..."
                    lbl_fname = tk.Label(
                        thumb_container,
                        text=fname,
                        font=("Segoe UI", 7),
                        fg=COLOR_TEXT_DIM,
                        bg=COLOR_BG_CARD
                    )
                    lbl_fname.pack()
                except:
                    pass

    def status(self, msg):
        print(f"[STATUS] {msg}")

    #==========================================
    #ACTIONS
    #==========================================

    def handle_keypress(self, event):
        key = event.keysym
        
        #Backspace = Undo last action
        if key == "BackSpace":
            self.undo_last_action()
            return
        
        #0 = Reject
        if key == "0":
            self.reject_image()
            return
        
        #R = Refresh with global-only (ignore metadata labels)
        if key.lower() == "r":
            self.refresh_global_only()
            return
            
        #1-9 = Accept corresponding candidate
        if key.isdigit():
            val = int(key)
            if val >= 1 and val <= 9:
                #Get actual candidates (skip section markers)
                actual_candidates = [c for c in self.current_candidates if c[0] != "__SECTION__"]
                idx = val - 1
                if idx < len(actual_candidates):
                    #Find index in original list for accept_image
                    cat_name = actual_candidates[idx][0]
                    orig_idx = next(i for i, c in enumerate(self.current_candidates) 
                                   if c[0] == cat_name)
                    self.accept_image(orig_idx)

    def accept_image(self, idx):
        cat_name = self.current_candidates[idx][0]
        
        target_dir = os.path.join(DIR_TARGET, cat_name)
        os.makedirs(target_dir, exist_ok=True)
        
        src = self.current_file_path
        dst = os.path.join(target_dir, os.path.basename(src))
        
        shutil.move(src, dst)
        self.status(f"Moved to {cat_name}")
        
        if cat_name not in self.reference_gallery:
            self.reference_gallery[cat_name] = []
        
        self.reference_gallery[cat_name].append({
            'emb': self.current_embedding,
            'path': dst
        })
        
        self.used_labels_in_sn.add(cat_name)
        
        #Record action for undo - include current used_labels state
        self.action_history.append({
            'src': src,
            'dst': dst,
            'label': cat_name,
            'type': 'accept',
            'sn': self.current_sn,
            'used_before': self.used_labels_in_sn.copy()  #Snapshot AFTER adding
        })
        self.completed_count += 1
        
        self.next_image()

    def reject_image(self):
        os.makedirs(DIR_REJECT, exist_ok=True)
        src = self.current_file_path
        dst = os.path.join(DIR_REJECT, os.path.basename(src))
        shutil.move(src, dst)
        self.status("Rejected.")
        
        #Record action for undo
        self.action_history.append({
            'src': src,
            'dst': dst,
            'label': None,
            'type': 'reject',
            'sn': self.current_sn,
            'used_before': self.used_labels_in_sn.copy()  #Snapshot current state
        })
        self.completed_count += 1
        
        self.next_image()
    
    def refresh_global_only(self):
        """Refresh current image with global-only guesses, ignoring metadata labels."""
        if not self.current_file_path or not os.path.exists(self.current_file_path):
            return
        
        if self.current_embedding is None:
            return
        
        self.status("Switched to global guesses only.")
        
        def score_cat(cat_name):
            refs = self.reference_gallery.get(cat_name, [])
            if not refs: 
                return (cat_name, 9999, [])
            
            dists = []
            for ref in refs:
                sim = torch.nn.functional.cosine_similarity(self.current_embedding, ref['emb'])
                dists.append(1 - sim.item())
            
            raw_avg = sum(dists) / len(dists)
            n = len(refs)
            shrinkage_factor = n / (n + SHRINKAGE_STRENGTH)
            avg_dist = shrinkage_factor * raw_avg + (1 - shrinkage_factor) * GLOBAL_MEAN_DIST
            
            #Popularity bonus: cats with more refs get slight distance reduction
            popularity_bonus = min(n * POPULARITY_BONUS_PER_REF, POPULARITY_BONUS_MAX)
            avg_dist = avg_dist - popularity_bonus
            
            ref_with_dist = []
            for ref in refs:
                sim = torch.nn.functional.cosine_similarity(self.current_embedding, ref['emb'])
                ref_with_dist.append((ref['path'], 1 - sim.item()))
            ref_with_dist.sort(key=lambda x: x[1])
            ref_paths = [r[0] for r in ref_with_dist[:4]]
            
            return (cat_name, avg_dist, ref_paths)
        
        #Score ALL cats in gallery (global search, no metadata filter)
        global_candidates = [cat for cat in self.reference_gallery.keys() 
                            if cat not in self.used_labels_in_sn]
        
        global_scored = []
        for cat in global_candidates:
            global_scored.append(score_cat(cat))
        global_scored.sort(key=lambda x: x[1])
        global_scored = global_scored[:9]  #Top 9 global guesses
        
        self.current_candidates = global_scored
        
        #Update display with global mode
        filename = os.path.basename(self.current_file_path)
        match = re.search(r'sn(\d+)', filename)
        sn_str = match.group(1) if match else "UNKNOWN"
        self.update_display(filename, sn_str, is_global=True)
    
    def undo_last_action(self):
        """Undo the last action: move the last sorted image back to ambiguous_or_groups."""
        if not self.action_history:
            self.status("Nothing to undo.")
            return
        
        #Pop the last action
        last_action = self.action_history.pop()
        src_original = last_action['src']  # Where it was (ambiguous)
        dst_sorted = last_action['dst']    # Where it went (target/reject)
        label = last_action['label']
        action_type = last_action['type']
        action_sn = last_action.get('sn')
        
        filename = os.path.basename(dst_sorted)
        
        #Move the file back to ambiguous_or_groups
        if os.path.exists(dst_sorted):
            restore_path = os.path.join(DIR_AMBIGUOUS, filename)
            shutil.move(dst_sorted, restore_path)
            self.status(f"Undo: Restored {filename} to ambiguous.")
            
            #If it was an accept, remove from gallery
            if action_type == 'accept' and label:
                if label in self.reference_gallery:
                    #Remove the last entry with this path
                    self.reference_gallery[label] = [
                        ref for ref in self.reference_gallery[label] 
                        if ref['path'] != dst_sorted
                    ]
            
            #Restore the used_labels state properly
            #If we're going back to the same SN, restore used_labels to state BEFORE this action
            if action_sn and action_sn == self.current_sn:
                #Remove the label that was just undone (if it was an accept)
                if action_type == 'accept' and label:
                    self.used_labels_in_sn.discard(label)
            elif action_sn:
                #Going back to a different SN - reconstruct used_labels
                #Look at previous history entries for this SN
                self.current_sn = action_sn
                self.used_labels_in_sn = set()
                for prev_action in self.action_history:
                    if prev_action.get('sn') == action_sn and prev_action.get('label'):
                        self.used_labels_in_sn.add(prev_action['label'])
            
            #Put current image back at the front of pending
            if self.current_file_path and os.path.exists(self.current_file_path):
                self.pending_files.insert(0, self.current_file_path)
            
            #Put the restored file at the front
            self.pending_files.insert(0, restore_path)
            
            #Decrement completed count
            self.completed_count = max(0, self.completed_count - 1)
            
            #Load the restored image
            self.next_image()
        else:
            self.status(f"Undo failed: {filename} not found at destination.")

if __name__ == "__main__":
    root = tk.Tk()
    app = CatSorterApp(root)
    root.mainloop()