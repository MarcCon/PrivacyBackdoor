Dieses Projekt demonstriert, wie man gezielt Backdoors in vortrainierte ML-Modelle (z. B. ViT) einpflanzen kann, um während des Fine-Tunings private Trainingsdaten zu stehlen.  
Rekonstruierte Bilder zeigen, dass selbst Pretrained Weights wie `ViT_B_32` ausreichen können, um aus Fine-Tuning-Schritten sensible Daten zurückzugewinnen.

 Die Original-Daten stammen aus dem [Caltech 101 Dataset](https://data.caltech.edu/records/mzrjq-6wc02).
 
 ---
 
 ## Setup & Installation
 
 ### 1. Repository klonen & virtuelle Umgebung einrichten
 
 Öffne dein Terminal und führe folgende Befehle aus:
 
 ```bash
 git clone <REPO_URL>
 cd PrivacyBackdoor
 
 python3 -m venv env
 source env/bin/activate 
 ```
 
 ### 2. Abhängigkeiten installieren
 
 ```bash
 pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
 pip install matplotlib opacus datasets transformers
 ```
 
 ---
 
 ## Modelle & Gewichte
 
 Lade die vortrainierten und finetuned Gewichte von diesem Google-Drive-Link herunter:
 
🔗 https://drive.google.com/drive/folders/1QAjlQqNFK2ZOqly_CglapgLSs-hn0NP5?usp=sharing
 
 Beispiel:  
 Die Datei `vit_stitch_gelu_caltech-001.pth` sollte im Ordner `./weights/` liegen.  
 Falls der Ordner noch nicht existiert, kannst du ihn mit folgendem Befehl erstellen:
 
 ```bash
 mkdir -p weights
 ```
 
 ---
 
 ## Bildrekonstruktion aus Fine-Tuned ViT
 
 Um rekonstruierte Bilder anzuzeigen, führe im Projektverzeichnis folgenden Befehl aus:

 ```bash
 python3 analysis/reconstruct_images.py \
   --path ./weights/vit_stitch_gelu_caltech-001.pth \
   --plot_mode recovery \
   --arch vit \
   --hw 4 8 \
   --inches 4.35 2.15 \
   --scaling 0.229 0.224 0.225 \
   --bias 0.485 0.456 0.406
 ```
 

