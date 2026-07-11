"""Storage local: CSV de eventos, JPEGs/TXT arquivados com rotação por espaço,
e JSONL de eventos com schema estável (costura p/ correlação de vizinhança v2)."""
import json, os, shutil, time


class Storage:
    def __init__(self, cfg, log):
        self.dir = cfg["storage"]["dir"]
        self.min_livre = cfg["storage"]["min_livre_pct"]
        self.log = log
        self.csv = os.path.join(self.dir, "deteccoes.csv")
        self.jsonl = os.path.join(self.dir, "eventos.jsonl")
        self.arquivo = os.path.join(self.dir, "arquivo")
        os.makedirs(self.arquivo, exist_ok=True)

    def registra(self, code, canal, extra="", desc=""):
        novo = not os.path.exists(self.csv)
        with open(self.csv, "a") as f:
            if novo:
                f.write("ts,code,canal,extra,descricao\n")
            d = desc.replace(",", ";").replace("\n", " ")
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{code},{canal},{extra},{d}\n")

    def evento_jsonl(self, **campos):
        """Schema estável: ts, canal, zona, tipo, classe, descricao, ficha.
        (v2 de vizinhança = enviar estas linhas a um endpoint comum.)"""
        campos.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        with open(self.jsonl, "a") as f:
            f.write(json.dumps(campos, ensure_ascii=False) + "\n")

    def salva(self, jpeg, texto, nome_base):
        """Grava par .jpg/.txt em arquivo/YYYY-MM/. Devolve caminho ou None."""
        self._rotaciona()
        subdir = os.path.join(self.arquivo, time.strftime("%Y-%m"))
        os.makedirs(subdir, exist_ok=True)
        base = os.path.join(subdir, f"{time.strftime('%Y%m%d-%H%M%S')}_{nome_base}")
        try:
            with open(base + ".jpg", "wb") as f:
                f.write(jpeg)
            with open(base + ".txt", "w") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{texto}\n")
            return base + ".jpg"
        except OSError as e:
            self.log(f"storage falhou ({e})")
            return None

    def _rotaciona(self):
        """Disco abaixo do mínimo → apaga os meses mais antigos do arquivo."""
        try:
            uso = shutil.disk_usage(self.dir)
            while uso.free / uso.total * 100 < self.min_livre:
                meses = sorted(d for d in os.listdir(self.arquivo)
                               if os.path.isdir(os.path.join(self.arquivo, d)))
                if len(meses) <= 1:
                    return
                alvo = os.path.join(self.arquivo, meses[0])
                shutil.rmtree(alvo)
                self.log(f"rotação: apagado {alvo} (disco < {self.min_livre}% livre)")
                uso = shutil.disk_usage(self.dir)
        except OSError:
            pass
