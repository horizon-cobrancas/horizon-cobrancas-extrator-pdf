from fastapi import FastAPI, UploadFile, File, HTTPException, status
import pdfplumber
import re
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel
import io

app = FastAPI(title="Condominium Data Extractor API")


# =====================================================================
# DTO (Pydantic Model) que reflete exatamente a sua classe Java 'Charge'
# =====================================================================
class ChargeJSON(BaseModel):
    extractCondominium: Optional[str] = None
    originalDraweeName: Optional[str] = None
    originalDraweeDocument: Optional[str] = None

    originalAmount: float = 0.0
    interestAmount: float = 0.0
    penaltyAmount: float = 0.0
    monetaryCorrection: float = 0.0
    legalFees: float = 0.0
    totalAmount: float = 0.0

    referenceMonth: Optional[str] = None
    dueDate: Optional[str] = None
    description: Optional[str] = None
    unit: Optional[str] = None


# =====================================================================
# FUNÇÕES UTILITÁRIAS
# =====================================================================
def parse_br_float(val: str) -> float:
    if not val or str(val).strip() == "": return 0.0
    v = str(val).replace("R$", "").strip()

    # Verifica se é um número negativo contábil (ex: "(2,97)" ou "-2,97")
    is_negative = False
    if v.startswith("(") and v.endswith(")"):
        is_negative = True
        v = v[1:-1] # Remove os parênteses
    elif v.startswith("-"):
        is_negative = True
        v = v[1:] # Remove o sinal de menos

    # Tratamento para pontuações de milhares e decimais do Brasil
    if "," in v and "." in v:
        if v.rfind(",") > v.rfind("."):
            v = v.replace(".", "").replace(",", ".")
        else:
            v = v.replace(",", "")
    elif "," in v:
        v = v.replace(",", ".")
    elif "." in v and len(v.split(".")[-1]) == 3:  # Ex: 1.887 virando 1887.0
        v = v.replace(".", "")

    try:
        result = float(v)
        return -result if is_negative else result
    except:
        return 0.0


def parse_br_date(val: str) -> Optional[str]:
    if not val or val.strip() == "": return None
    v = val.strip()
    try:
        if len(v) == 8:  # Ex: 15/02/26
            return datetime.strptime(v, "%d/%m/%y").strftime("%Y-%m-%d")
        return datetime.strptime(v, "%d/%m/%Y").strftime("%Y-%m-%d")
    except:
        return v

@app.get("/")
def read_root():
    return {"status": "alive"}

@app.post("/v1/extract/condominio21", response_model=List[ChargeJSON])
async def extract_condominio21(file: UploadFile = File(...)):
    """
    Extração Universal Condomínio21 / Group Software.
    Suporta dinamicamente os 48 condomínios da mesma família.
    """
    pdf_bytes = await file.read()
    charges = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

        # ==========================================
        # ABORDAGEM DEFENSIVA: Validação de Layout (Fail-Fast)
        # ==========================================
        if len(pdf.pages) > 0:
            first_page_text = pdf.pages[0].extract_text() or ""

            # 1. Digital do UCondo
            if "Relatório de Inadimplentes" in first_page_text and "Pendência" in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "Você selecionou Condomínio21, mas este arquivo pertence ao UCondo."}
                )

            # 2. Digital do Condomob
            if "Data de referência:" in first_page_text and "Pagador:" in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "Você selecionou Condomínio21, mas este arquivo pertence ao Condomob."}
                )

            # 3. Digital do Superlógica
            if "Valores atualizados até" in first_page_text and "Compet." in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "Você selecionou Condomínio21, mas este arquivo pertence ao Superlógica."}
                )

            # 4. Validação Positiva (Opcional, mas muito recomendada)
            # Se não tem a assinatura nativa do Condomínio21, rejeita também!
            if "UNIDADES INADIMPLENTES" not in first_page_text and "Data Base:" not in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "O arquivo enviado não possui o formato esperado do Condomínio21 / Group Software."}
                )
        # ==========================================

    # Memória global para lidar com quebras de página
    current_condominium = None
    current_unit = None
    current_name = None

    # Flag para armadilha de captura do nome do condomínio
    capture_condo_name = False

    # Blacklist limpa (removemos os nomes dos condomínios fixos)
    # Adicionamos "Unidade" e "Classe de Conta" para evitar falsos positivos de leitura
    blacklist = [
        "Data Base", "Condominio21", "Group Software",
        "Inadimplência", "Mês: todos", "Correção:", "Juros:",
        "Pág:", "Mês Ref", "Proj. Rec.", "HONORÁRIOS DE COBRANÇAS",
        "Mês Ref Vencimento", "Unidade", "Classe de Conta"
    ]

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(layout=True)
            if not text: continue

            lines = [re.sub(r'\s+', ' ', line.strip()) for line in text.split('\n') if line.strip()]

            # Padrão universal para a linha financeira
            pattern = re.compile(
                r"^(.*?)\s+(\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)$"
            )

            for line in lines:
                # ==========================================
                # CAPTURA DINÂMICA DO CONDOMÍNIO
                # ==========================================
                if "UNIDADES INADIMPLENTES" in line:
                    capture_condo_name = True
                    continue

                if capture_condo_name:
                    if line.strip() != "":
                        current_condominium = line.strip()
                        capture_condo_name = False
                    continue
                # ==========================================

                # 1. Ignora as linhas sujas usando a blacklist
                if any(garbage in line for garbage in blacklist):
                    continue

                # 2. Reset de nome para casos de Sub-locação (Herança do JK)
                if line.startswith("Total"):
                    if "Total '" in line:
                        current_name = None
                    continue

                match = pattern.match(line)

                if match:
                    desc = match.group(1).strip()

                    # Trata descrições vazias (Acordos do JK)
                    if desc == "": desc = "Acordo / Diversos"
                    if desc.startswith("Total"): continue

                    # Extração dos valores para cálculo
                    v_original = parse_br_float(match.group(4))
                    v_juros = parse_br_float(match.group(5))
                    v_multa = parse_br_float(match.group(6))
                    v_correcao = parse_br_float(match.group(7))
                    v_pdf_total = parse_br_float(match.group(8))

                    # Regra de Negócio: Honorários = 10%
                    v_honorarios = round((v_original + v_juros + v_multa + v_correcao) * 0.1, 2)
                    v_total_final = round(v_pdf_total + v_honorarios, 2)

                    charges.append(ChargeJSON(
                        extractCondominium=current_condominium,  # <- INJETADO NA SAÍDA
                        unit=current_unit,
                        originalDraweeName=current_name,
                        description=desc,
                        referenceMonth=match.group(2),
                        dueDate=parse_br_date(match.group(3)),
                        originalAmount=v_original,
                        interestAmount=v_juros,
                        penaltyAmount=v_multa,
                        monetaryCorrection=v_correcao,
                        legalFees=v_honorarios,
                        totalAmount=v_total_final
                    ))
                else:
                    # 3. Detecção de Unidade Flexível (Lojas, Salas, Apartamentos Numéricos ou Alfanuméricos)
                    is_unit = False
                    line_lower = line.lower()

                    if line_lower.startswith("loja ") or line_lower.startswith("sala "):
                        if re.match(r"^(loja|sala)\s+[a-z0-9-]+$", line_lower):
                            is_unit = True
                    elif re.match(r"^[A-Za-z0-9-]{2,8}$", line) and " " not in line:
                        is_unit = True

                    if is_unit:
                        current_unit = line
                        current_name = None  # Reseta o nome para o novo morador

                    # 4. Detecção e Limpeza do Nome do Morador
                    elif current_unit:
                        if (not current_name) or (current_unit in line):
                            clean_name = line

                            if current_unit in clean_name:
                                clean_name = clean_name.replace(current_unit, "")

                            clean_name = re.sub(r'[-\s]+[A-Za-z]-\d{2,4}$', '', clean_name)
                            clean_name = re.sub(r'[-\s]+$', '', clean_name).strip()

                            if len(clean_name) > 2 and not re.match(r'^[\d/.,\s-]+$', clean_name):
                                current_name = clean_name

    return charges

@app.post("/v1/extract/plenna", response_model=List[ChargeJSON])
async def extract_plenna(file: UploadFile = File(...)):
    """ Extração padrão Condominio21 (Plenna) """
    pdf_bytes = await file.read()
    charges = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(layout=True)
            if not text: continue

            lines = [re.sub(r'\s+', ' ', line.strip()) for line in text.split('\n') if line.strip()]
            current_unit, current_name = None, None

            # Padrão: [Descrição] [Mês] [Vencimento] [Valor] [Juros] [Multa] [Correção] [Total]
            pattern = re.compile(
                r"^(.*?)\s+(\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)$"
            )

            for line in lines:
                if line.startswith("UNIDADES INADIMPLENTES") or line.startswith(
                    "Total") or "Data Base" in line: continue

                match = pattern.match(line)
                if match:
                    desc = match.group(1).strip()
                    if desc.startswith("Total"): continue
                    charges.append(ChargeJSON(
                        unit=current_unit,
                        originalDraweeName=current_name,
                        description=desc,
                        referenceMonth=match.group(2),
                        dueDate=parse_br_date(match.group(3)),
                        originalAmount=parse_br_float(match.group(4)),
                        interestAmount=parse_br_float(match.group(5)),
                        penaltyAmount=parse_br_float(match.group(6)),
                        monetaryCorrection=parse_br_float(match.group(7)),
                        totalAmount=parse_br_float(match.group(8))
                    ))
                else:
                    if re.match(r"^[A-Za-z0-9-]{2,6}$", line) and " " not in line:
                        current_unit, current_name = line, None
                    elif current_unit and not current_name:
                        current_name = line
    return charges


@app.post("/v1/extract/condominiojk", response_model=List[ChargeJSON])
async def extract_condominiojk(file: UploadFile = File(...)):
    """ Extração Condominio21 (Condominio JK) - Adaptado para Multi-Pagadores e Acordos sem Descrição """
    pdf_bytes = await file.read()
    charges = []

    current_unit, current_name = None, None

    blacklist = [
        "UNIDADES INADIMPLENTES",
        "Data Base",
        "Condominio21",
        "Group Software",
        "CONDOMINIO DO EDIFICIO JK",
        "Inadimplência",
        "Mês: todos",
        "Correção:",
        "Juros:",
        "Pág:",
        "Mês Ref",
        "Proj. Rec.",
        "HONORÁRIOS DE COBRANÇAS"
    ]

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(layout=True)
            if not text: continue

            lines = [re.sub(r'\s+', ' ', line.strip()) for line in text.split('\n') if line.strip()]

            # Adicionado o () dentro da busca dos valores numéricos para suportar negativos contábeis
            pattern = re.compile(
                r"^(.*?)\s+(\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)$"
            )

            for line in lines:
                if any(garbage in line for garbage in blacklist):
                    continue

                if line.startswith("Total"):
                    # O sistema imprime "Total 'NOME':" no fim do bloco.
                    if "Total '" in line:
                        current_name = None
                    continue

                match = pattern.match(line)

                if match:
                    desc = match.group(1).strip()

                    # Se não houver descrição, é um bloco de Acordo/Renegociação
                    if desc == "":
                        desc = "Acordo / Diversos"

                    if desc.startswith("Total"): continue

                    charges.append(ChargeJSON(
                        unit=current_unit,
                        originalDraweeName=current_name,
                        description=desc,
                        referenceMonth=match.group(2),
                        dueDate=parse_br_date(match.group(3)),
                        originalAmount=parse_br_float(match.group(4)),
                        interestAmount=parse_br_float(match.group(5)),
                        penaltyAmount=parse_br_float(match.group(6)),
                        monetaryCorrection=parse_br_float(match.group(7)),
                        totalAmount=parse_br_float(match.group(8))
                    ))
                else:
                    is_unit = False
                    line_lower = line.lower()

                    if line_lower.startswith("loja ") or line_lower.startswith("sala "):
                        if re.match(r"^(loja|sala)\s+[a-z0-9-]+$", line_lower):
                            is_unit = True
                    elif re.match(r"^[A-Za-z0-9-]{2,8}$", line) and " " not in line:
                        is_unit = True

                    if is_unit:
                        current_unit = line
                        current_name = None

                    elif current_unit:
                        # Se não tinha nome, ou se a unidade está contida na linha atual (sub-pagador)
                        if (not current_name) or (current_unit in line):
                            clean_name = line

                            if current_unit in clean_name:
                                clean_name = clean_name.replace(current_unit, "")
                            clean_name = re.sub(r'[-\s]+$', '', clean_name).strip()

                            # Blindagem para evitar que datas órfãs ou lixo numérico virem nome de morador
                            if len(clean_name) > 2 and not re.match(r'^[\d/.,\s-]+$', clean_name):
                                current_name = clean_name

    return charges

@app.post("/v1/extract/condopalmas", response_model=List[ChargeJSON])
async def extract_condopalmas(file: UploadFile = File(...)):
    """ Extração Condominio21 (CondoPalmas) - Correção de página e limpeza de nomes """
    pdf_bytes = await file.read()
    charges = []

    # FIX 1: Tiramos as variáveis de estado de dentro do loop de páginas.
    # Agora elas sobrevivem à virada de página do PDF!
    current_unit, current_name = None, None

    with (pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf):
        for page in pdf.pages:
            text = page.extract_text(layout=True)
            if not text: continue

            lines = [re.sub(r'\s+', ' ', line.strip()) for line in text.split('\n') if line.strip()]

            # Padrão para capturar as cobranças (Descrição, Mês, Vencimento, e os 5 valores)
            pattern = re.compile(
                r"^(.*?)\s+(\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)$"
            )

            blacklist = [
                "UNIDADES INADIMPLENTES",
                "Data Base",
                "Condominio21",
                "Group Software",
                "Condo27 Administra",
                "Residencial Luar do Cerrado",
                "Inadimplência",
                "Mês: todos",
                "Correção:",
                "Juros:",
                "Pág:",
                "Mês Ref Vencimento",
                "Proj. Rec."
            ]

            for line in lines:
                # 1. Ignora as linhas sujas usando a blacklist
                if any(garbage in line for garbage in blacklist) or line.startswith("Total"):
                    continue

                match = pattern.match(line)

                if match:
                    desc = match.group(1).strip()
                    if desc.startswith("Total") or desc == "": continue

                    charges.append(ChargeJSON(
                        unit=current_unit,
                        originalDraweeName=current_name,
                        description=desc,
                        referenceMonth=match.group(2),
                        dueDate=parse_br_date(match.group(3)),
                        originalAmount=parse_br_float(match.group(4)),
                        interestAmount=parse_br_float(match.group(5)),
                        penaltyAmount=parse_br_float(match.group(6)),
                        monetaryCorrection=parse_br_float(match.group(7)),
                        totalAmount=parse_br_float(match.group(8))
                    ))
                else:
                    # Verifica se a linha é apenas a Unidade (Ex: A-04, B-22)
                    if re.match(r"^[A-Za-z0-9-]{2,8}$", line) and " " not in line:
                        current_unit = line
                        current_name = None  # Reseta o nome para o novo morador

                    # Se já temos uma unidade guardada, mas não temos o nome, esta linha é o nome!
                    elif current_unit and not current_name:
                        clean_name = line

                        # FIX 2: Sequência de limpeza do nome agressiva
                        # 2.1 - Remove a unidade exata caso esteja no meio/fim da string
                        if current_unit in clean_name:
                            clean_name = clean_name.replace(current_unit, "")

                        # 2.2 - Remove variações de blocos no final (Ex: " - B-004" quando a unidade é "B-04")
                        clean_name = re.sub(r'[-\s]+[A-Za-z]-\d{2,4}$', '', clean_name)

                        # 2.3 - Remove hifens e espaços solitários que sobraram no final (Ex: "João da Silva -")
                        clean_name = re.sub(r'[-\s]+$', '', clean_name).strip()

                        current_name = clean_name

    return charges


@app.post("/v1/extract/superlogica", response_model=List[ChargeJSON])
async def extract_superlogica(file: UploadFile = File(...)):
    """ Extração padrão Superlogica """
    pdf_bytes = await file.read()
    charges = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

        # ==========================================
        # ABORDAGEM DEFENSIVA: Validação de Layout (Fail-Fast)
        # ==========================================
        if len(pdf.pages) > 0:
            first_page_text = pdf.pages[0].extract_text() or ""

            # 1. Digital do UCondo
            if "Relatório de Inadimplentes" in first_page_text and "Pendência" in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "Você selecionou Superlógica, mas este arquivo pertence ao UCondo."}
                )

            # 2. Digital do Condomob
            if "Data de referência:" in first_page_text and "Pagador:" in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "Você selecionou Superlógica, mas este arquivo pertence ao Condomob."}
                )

            # 3. Digital do Condomínio21
            if "UNIDADES INADIMPLENTES" in first_page_text and "Data Base:" in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "Você selecionou Superlógica, mas este arquivo pertence ao Condomínio21."}
                )

            # 4. Validação Positiva
            if "Valores atualizados até" not in first_page_text and "Compet." not in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "O arquivo enviado não possui o formato esperado do Superlógica."}
                )
        # ==========================================

    current_unit, current_name = None, None
    current_condominium = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Regex da Dívida
        pattern = re.compile(
            r"^(\d{2}/\d{2}/\d{2,4})\s+(\d{2}/\d{4})\s+(\d+)\s+(\d+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)$"
        )

        # MODELO 1: Padrão antigo que você já tinha (Ex: "101 Unidade - JULIANA" ou "101 Unidade JULIANA")
        unit_pattern_legacy = re.compile(r"^([A-Za-z0-9-]+)\s+Unidade\s+(?:-\s+)?(.*)$", re.IGNORECASE)

        # MODELO 2: Padrão novo/Berlim (Ex: "101 BLOCO 1 JULIANA" ou apenas "101 JULIANA")
        unit_pattern_new = re.compile(r"^([A-Za-z0-9-]+)\s+(?:BLOCO\s+([A-Za-z0-9]+)\s+)?(.*)$", re.IGNORECASE)

        for page in pdf.pages:
            text = page.extract_text(layout=True)
            if not text: continue

            lines = [re.sub(r'\s+', ' ', line.strip()) for line in text.split('\n') if line.strip()]

            for idx, line in enumerate(lines):

                # ==========================================
                # CAPTURA DO CONDOMÍNIO
                # ==========================================
                if "Inadimplentes" in line and not current_condominium:
                    if idx > 0:
                        raw_condo = lines[idx - 1].strip()
                        clean_condo = re.sub(r"^[A-Za-z0-9]+\s+", "", raw_condo)
                        clean_condo = re.sub(r"\s*\(\d+\)$", "", clean_condo)
                        current_condominium = clean_condo.strip()

                # Ignora cabeçalhos, totalizadores e linhas de metadados da Superlógica
                if "Vencimento" in line or line.startswith(
                        "Total") or "Inadimplentes" in line or "Valores atualizados" in line:
                    continue

                # Proteção extra: Impede que a primeira linha (nome do condomínio) seja confundida com um morador
                if current_condominium and current_condominium in line:
                    continue

                # ==========================================
                # IDENTIFICA TROCA DE UNIDADE/MORADOR
                # ==========================================

                # Tentativa 1: Modelo Legado (com a palavra "Unidade")
                u_match_legacy = unit_pattern_legacy.match(line)
                if u_match_legacy:
                    current_unit = u_match_legacy.group(1).strip()
                    raw_name = u_match_legacy.group(2).strip()
                    current_name = re.sub(r'\s+(Jurídico|Acordo|Cobrança)$', '', raw_name, flags=re.IGNORECASE).strip()
                    continue

                # Tentativa 2: Modelo Novo (Com ou sem Bloco)
                # O "not re.match" garante que a gente não vai processar uma linha de dívida por engano aqui
                if not re.match(r'^\d{2}/\d{2}', line):
                    u_match_new = unit_pattern_new.match(line)
                    if u_match_new:
                        unidade = u_match_new.group(1).strip()

                        # Blindagem: A unidade geralmente tem até uns 8 caracteres.
                        # Isso evita capturar uma frase solta no PDF que não seja um morador.
                        if len(unidade) <= 10:
                            bloco = u_match_new.group(2)
                            raw_name = u_match_new.group(3).strip()

                            if bloco:
                                current_unit = f"{bloco.strip()}-{unidade}"
                            else:
                                current_unit = unidade

                            current_name = re.sub(r'\s+(Jurídico|Acordo|Cobrança)$', '', raw_name,
                                                  flags=re.IGNORECASE).strip()
                            continue

                # ==========================================
                # EXTRAÇÃO DA DÍVIDA
                # ==========================================
                match = pattern.match(line)
                # Só anexa a dívida se tivermos conseguido ancorar uma unidade a ela
                if match and current_unit:
                    charges.append(ChargeJSON(
                        extractCondominium=current_condominium,
                        unit=current_unit,
                        originalDraweeName=current_name,
                        dueDate=parse_br_date(match.group(1)),
                        referenceMonth=match.group(2),
                        documentNumber=match.group(4),
                        originalAmount=parse_br_float(match.group(5)),
                        interestAmount=parse_br_float(match.group(6)),
                        penaltyAmount=parse_br_float(match.group(7)),
                        monetaryCorrection=parse_br_float(match.group(8)),
                        legalFees=parse_br_float(match.group(9)),
                        totalAmount=parse_br_float(match.group(10))
                    ))

    return charges

def extract_ref_month(desc: str) -> Optional[str]:
    """ Converte strings como 'Ref. Dezembro de 2024' para '12/2024' """
    months = {
        "janeiro": "01", "fevereiro": "02", "março": "03", "marco": "03",
        "abril": "04", "maio": "05", "junho": "06", "julho": "07",
        "agosto": "08", "setembro": "09", "outubro": "10",
        "novembro": "11", "dezembro": "12"
    }
    match = re.search(
        r"(?i)(janeiro|fevereiro|março|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})",
        desc)
    if match:
        m = match.group(1).lower()
        y = match.group(2)
        return f"{months[m]}/{y}"
    return None


@app.post("/v1/extract/ucondo", response_model=List[ChargeJSON])
async def extract_ucondo(file: UploadFile = File(...)):
    """ Extração UCondo (Máquina de Extração por Descascamento de Linha) """
    pdf_bytes = await file.read()
    charges = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

        # ==========================================
        # ABORDAGEM DEFENSIVA: Validação de Layout (Fail-Fast)
        # ==========================================
        if len(pdf.pages) > 0:
            first_page_text = pdf.pages[0].extract_text() or ""

            # 1. Digital do Superlógica
            if "Valores atualizados até" in first_page_text and "Compet." in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "Você selecionou UCondo, mas este arquivo pertence ao Superlógica."}
                )

            # 2. Digital do Condomob
            if "Data de referência:" in first_page_text and "Pagador:" in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "Você selecionou UCondo, mas este arquivo pertence ao Condomob."}
                )

            # 3. Digital do Condomínio21
            if "UNIDADES INADIMPLENTES" in first_page_text and "Data Base:" in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "Você selecionou UCondo, mas este arquivo pertence ao Condomínio21."}
                )

            # 4. Validação Positiva (Opcional, mas muito recomendada)
            # Se não tem a assinatura nativa do UCondo, rejeita também!
            if "Relatório de Inadimplentes" not in first_page_text and "Pendência" not in first_page_text:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "LAYOUT_MISMATCH",
                            "message": "O arquivo enviado não possui o formato esperado do UCondo."}
                )
        # ==========================================

    current_unit, current_name, current_cpf = None, None, None
    current_condominium = None  # <-- Memória do Condomínio
    capture_condo_name = False  # <-- Armadilha UCondo
    current_block_charges = []

    pattern = re.compile(
        r"^(.*?)\s+(\d{2}/\d{2}/\d{4})\s+R\$\s*([()\-\d.,]+)\s+R\$\s*([()\-\d.,]+)\s+R\$\s*([()\-\d.,]+)\s+R\$\s*([()\-\d.,]+)\s+R\$\s*([()\-\d.,]+)\s+R\$\s*([()\-\d.,]+)$"
    )

    blacklist = [
        "Relatório", "Período", "Gerado", "Página",
        "Pendência", "Vencimento", "Valor Original"
    ]

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(layout=True)
            if not text: continue

            lines = [re.sub(r'\s+', ' ', line.strip()) for line in text.split('\n') if line.strip()]

            skip_header_mode = False
            skip_footer_mode = False

            for line in lines:
                temp_line = line

                # 1. Ignora o Rodapé
                if "Gerado em" in temp_line:
                    skip_footer_mode = True
                if skip_footer_mode:
                    continue

                # ==========================================
                # 2. CAPTURA DO CONDOMÍNIO E IGNORA CABEÇALHO
                # ==========================================
                if "Relatório" in temp_line or "Inadimplentes" in temp_line:
                    if "Relatório de Inadimplentes" in temp_line and not current_condominium:
                        capture_condo_name = True
                    skip_header_mode = True
                    continue

                # Se a armadilha está armada, a linha atual é o nome do condomínio!
                if capture_condo_name:
                    if temp_line.strip() and "Período" not in temp_line:
                        current_condominium = temp_line.strip()
                        capture_condo_name = False
                    # Não damos continue aqui, deixamos o skip_header_mode (abaixo) engolir a linha

                # Continua pulando o cabeçalho até achar o Fim dele ("Período")
                if skip_header_mode:
                    if "Período" in temp_line:
                        skip_header_mode = False
                    continue
                # ==========================================

                # 3. Ignora cabeçalhos de tabela isolados
                if "Pendência" in temp_line or "Valor Original" in temp_line or temp_line.startswith("Total"):
                    continue

                # 4. Início de um novo morador (Descasca a Unidade)
                if "Bloco" in temp_line and "apto" in temp_line:
                    charges.extend(current_block_charges)
                    current_block_charges = []

                    unit_match = re.search(r"Bloco\s+\S+\s+apto\s+\S+", temp_line)
                    if unit_match:
                        current_unit = unit_match.group(0)
                        temp_line = temp_line.replace(current_unit, "")
                    else:
                        current_unit = temp_line.strip()
                        temp_line = ""

                    current_name, current_cpf = None, None

                # 5. Captura de CPF/CNPJ (Descasca o Documento)
                cpf_match = re.search(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", temp_line)
                if not cpf_match:
                    cpf_match = re.search(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b", temp_line)

                if cpf_match:
                    current_cpf = cpf_match.group(0)
                    for c in current_block_charges:
                        c.originalDraweeDocument = current_cpf
                    temp_line = temp_line.replace(current_cpf, "")

                    # 6. Elimina lixos visuais de contato
                    email_match = re.search(r"\S+@\S*", temp_line)
                    if email_match:
                        temp_line = temp_line.replace(email_match.group(0), "")

                    phone_match = re.search(r"\(\d{2}\)\s*\d{4,5}-\d{4}", temp_line)
                    if phone_match:
                        temp_line = temp_line.replace(phone_match.group(0), "")

                # 7. Captura a Linha de Cobrança
                match = pattern.match(line)
                if match:
                    desc = match.group(1).strip()
                    ref_month = extract_ref_month(desc)

                    charge = ChargeJSON(
                        extractCondominium=current_condominium,  # <-- INJETADO NA SAÍDA
                        unit=current_unit,
                        originalDraweeName=current_name,
                        originalDraweeDocument=current_cpf,
                        description=desc,
                        referenceMonth=ref_month,
                        dueDate=parse_br_date(match.group(2)),
                        originalAmount=parse_br_float(match.group(3)),
                        monetaryCorrection=parse_br_float(match.group(4)),
                        penaltyAmount=parse_br_float(match.group(5)),
                        interestAmount=parse_br_float(match.group(6)),
                        legalFees=parse_br_float(match.group(7)),
                        totalAmount=parse_br_float(match.group(8))
                    )
                    current_block_charges.append(charge)
                    continue

                    # 8. Finalmente, o que restou na linha "descascada" é o Nome!
                temp_line = temp_line.strip()
                if current_unit and not current_name:
                    if temp_line and "R$" not in temp_line:
                        if not re.match(r"^[\d\.\-\/\s:,]+$", temp_line):
                            if not any(garbage in temp_line for garbage in blacklist):
                                current_name = temp_line
                                # Atualização retroativa das cobranças órfãs
                                for c in current_block_charges:
                                    c.originalDraweeName = current_name

    charges.extend(current_block_charges)
    return charges


@app.post("/v1/extract/condomob", response_model=List[ChargeJSON])
async def extract_condomob(file: UploadFile = File(...)):
    """ Extração Condomob (Resolução de Pagadores e Unidade via Totalizador Retroativo) """
    pdf_bytes = await file.read()
    charges = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

        # ==========================================
        # ABORDAGEM DEFENSIVA: Validação de Layout (Fail-Fast)
        # ==========================================
        if len(pdf.pages) > 0:
            first_page_text = pdf.pages[0].extract_text() or ""

            if "Valores atualizados até" in first_page_text and "Compet." in first_page_text:
                raise HTTPException(status_code=422, detail={"error": "LAYOUT_MISMATCH",
                                                             "message": "Você selecionou Condomob, mas este arquivo pertence ao Superlógica."})

            if "Relatório de Inadimplentes" in first_page_text and "Pendência" in first_page_text:
                raise HTTPException(status_code=422, detail={"error": "LAYOUT_MISMATCH",
                                                             "message": "Você selecionou Condomob, mas este arquivo pertence ao UCondo."})

            if "UNIDADES INADIMPLENTES" in first_page_text and "Data Base:" in first_page_text:
                raise HTTPException(status_code=422, detail={"error": "LAYOUT_MISMATCH",
                                                             "message": "Você selecionou Condomob, mas este arquivo pertence ao Condomínio21."})

            if "Data de referência:" not in first_page_text and "Pagador:" not in first_page_text:
                raise HTTPException(status_code=422, detail={"error": "LAYOUT_MISMATCH",
                                                             "message": "O arquivo enviado não possui o formato esperado do Condomob."})
        # ==========================================

    current_condominium = None
    current_prop_name, current_prop_cpf = None, None
    current_inq_name, current_inq_cpf = None, None
    current_pagador = "proprietario"

    current_block_data = []

    end_pattern = re.compile(
        r"(?:(\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(\d+)|(\d{2}/\d{2}/\d{4})\s+(\d+)\s+(\d{2}/\d{4}))\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)\s+([()\d.,]+)$"
    )

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            text = page.extract_text(layout=True)
            if not text: continue

            lines = [re.sub(r'\s+', ' ', line.strip()) for line in text.split('\n') if line.strip()]

            # ==========================================
            # CAPTURA DO CONDOMÍNIO
            # ==========================================
            if page_num == 0 and not current_condominium and len(lines) > 0:
                raw_condo = lines[0].strip()
                clean_condo = re.split(r'(?i)(Inadimplência|Data de)', raw_condo)[0]
                current_condominium = clean_condo.strip()
            # ==========================================

            for line in lines:
                if "Inadimplência" in line or "Data de" in line or line.startswith(
                        "Tipo") or line == current_condominium:
                    continue

                # ==========================================
                # CAPTURA DE PAGADORES
                # ==========================================
                pag_match = re.search(r"Pagador:\s*([A-Za-z]+)", line, re.IGNORECASE)
                if pag_match:
                    current_pagador = pag_match.group(1).strip().lower()

                prop_match = re.search(
                    r"Proprietário:\s+(.*?)(?:\s*\(([\d\.\-\/]+)\))?(?=\s*(?:Pagador|Inquilino|$))", line,
                    re.IGNORECASE)
                if prop_match:
                    current_prop_name = prop_match.group(1).strip()
                    current_prop_cpf = prop_match.group(2).strip() if prop_match.group(2) else None

                inq_match = re.search(
                    r"Inquilino:\s+(.*?)(?:\s*\(([\d\.\-\/]+)\))?(?=\s*(?:Pagador|Proprietário|$))", line,
                    re.IGNORECASE)
                if inq_match:
                    current_inq_name = inq_match.group(1).strip()
                    current_inq_cpf = inq_match.group(2).strip() if inq_match.group(2) else None

                if pag_match or prop_match or inq_match:
                    continue
                # ==========================================

                # =========================================================
                # CAPTURA DA UNIDADE NO TOTALIZADOR
                # =========================================================
                tot_match = re.search(r"^([A-Za-z0-9-*]+)\s*:\s*\d+\s+cobrança", line, re.IGNORECASE)
                if tot_match:
                    found_unit = tot_match.group(1).replace("*", "").strip()
                    for data in current_block_data:
                        data["unit"] = found_unit
                        charges.append(ChargeJSON(**data))
                    current_block_data = []
                    continue

                match = end_pattern.search(line)
                if match:
                    prefix = line[:match.start()].strip()
                    parts = prefix.split()

                    desc = parts[0] if parts else "Ordinária"
                    doc_number = parts[-1] if len(parts) > 1 else ""

                    installment = ""
                    for p in parts:
                        if re.match(r"^\d+/\d+$", p):
                            installment = p
                            break

                    ref_month = match.group(1) or match.group(6)
                    due_date_str = match.group(2) or match.group(4)

                    if "inquilino" in current_pagador and current_inq_name:
                        drawee_name = current_inq_name
                        drawee_doc = current_inq_cpf
                    else:
                        drawee_name = current_prop_name
                        drawee_doc = current_prop_cpf

                    current_block_data.append({
                        "extractCondominium": current_condominium,
                        "unit": None,
                        "originalDraweeName": drawee_name,

                        "originalDraweeDocument": format_document(drawee_doc),

                        "description": desc,
                        "documentNumber": doc_number,
                        "installment": installment,
                        "referenceMonth": ref_month,
                        "dueDate": parse_br_date(due_date_str),
                        "originalAmount": parse_br_float(match.group(7)),
                        "penaltyAmount": parse_br_float(match.group(8)),
                        "interestAmount": parse_br_float(match.group(9)),
                        "monetaryCorrection": parse_br_float(match.group(10)),
                        "legalFees": parse_br_float(match.group(11)),
                        "totalAmount": parse_br_float(match.group(12))
                    })

    if current_block_data:
        for data in current_block_data:
            charges.append(ChargeJSON(**data))

    return charges

def format_document(doc_str: str) -> str | None:
    """ Limpa a string e garante 11 dígitos para CPF ou 14 dígitos para CNPJ. """
    if not doc_str:
        return None

    # Remove tudo que não for número
    clean_doc = re.sub(r"\D", "", doc_str)

    if not clean_doc:
        return None

    # Completa com zeros à esquerda dependendo do tamanho
    if len(clean_doc) <= 11:
        return clean_doc.zfill(11)  # CPF: Garante 11 dígitos
    elif len(clean_doc) <= 14:
        return clean_doc.zfill(14)  # CNPJ: Garante 14 dígitos

    return clean_doc  # Retorna como está se for uma anomalia maior que 14