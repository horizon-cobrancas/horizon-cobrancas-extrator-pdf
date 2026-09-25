from fastapi import FastAPI, UploadFile, File, HTTPException, status
import pdfplumber
import re
from typing import List, Optional
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import unicodedata
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


def clean_superlogica_name(raw_name: str) -> str:
    name = re.sub(r"^[-–—]\s*", "", raw_name.strip())
    return re.sub(
        r"(?:\s*[-–—\uFFFD]\s*|\s+)(?:Jurídico|Acordo|Cobrança)$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()


def normalize_layout_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def parse_br_decimal(value: str) -> Decimal:
    cleaned = value.strip()
    is_negative = cleaned.startswith("-") or (cleaned.startswith("(") and cleaned.endswith(")"))
    cleaned = cleaned.strip("()-").replace(".", "").replace(",", ".")
    amount = Decimal(cleaned)
    return -amount if is_negative else amount


def raise_superlogica_layout_error(reason: str, page_number: int = None, line_number: int = None):
    location = ""
    if page_number is not None:
        location = f" Página {page_number}"
        if line_number is not None:
            location += f", linha {line_number}"
        location += "."

    raise HTTPException(
        status_code=422,
        detail={
            "error": "LAYOUT_MISMATCH",
            "message": (
                "A estrutura do PDF da Superlógica mudou ou está incompleta. "
                f"A extração foi cancelada para evitar dados incorretos.{location} Motivo: {reason}"
            ),
        },
    )


def raise_condominio21_layout_error(reason: str, page_number: int = None, line_number: int = None):
    location = ""
    if page_number is not None:
        location = f" Página {page_number}"
        if line_number is not None:
            location += f", linha {line_number}"
        location += "."

    raise HTTPException(
        status_code=422,
        detail={
            "error": "LAYOUT_MISMATCH",
            "message": (
                "A estrutura do PDF do Condomínio 21 mudou ou está incompleta. "
                f"A extração foi cancelada para evitar dados incorretos.{location} Motivo: {reason}"
            ),
        },
    )


def raise_condomob_layout_error(reason: str, page_number: int = None, line_number: int = None):
    location = ""
    if page_number is not None:
        location = f" Página {page_number}"
        if line_number is not None:
            location += f", linha {line_number}"
        location += "."

    raise HTTPException(
        status_code=422,
        detail={
            "error": "LAYOUT_MISMATCH",
            "message": (
                "A estrutura do PDF do Condomob mudou ou está incompleta. "
                f"A extração foi cancelada para evitar dados incorretos.{location} Motivo: {reason}"
            ),
        },
    )


def raise_ucondo_layout_error(reason: str, page_number: int = None, line_number: int = None):
    location = ""
    if page_number is not None:
        location = f" Página {page_number}"
        if line_number is not None:
            location += f", linha {line_number}"
        location += "."

    raise HTTPException(
        status_code=422,
        detail={
            "error": "LAYOUT_MISMATCH",
            "message": (
                "A estrutura do PDF do UCondo mudou ou está incompleta. "
                f"A extração foi cancelada para evitar dados incorretos.{location} Motivo: {reason}"
            ),
        },
    )


def canonical_condominio21_unit(value: str) -> str:
    compact = re.sub(r"[^a-z0-9]", "", normalize_layout_text(value))
    return re.sub(r"\d+", lambda match: str(int(match.group(0))), compact)


def clean_condominio21_name(raw_name: str, unit: str) -> str:
    name = raw_name.strip()
    suffix_patterns = [
        r"(?:\s*-\s*|\s+)(Apto\s*\d+)$",
        r"(?:\s*-\s*|\s+)([A-Za-z]+(?:[-\s]?\d+))$",
        r"(?:\s*-\s*|\s+)(\d+)$",
    ]
    for suffix_pattern in suffix_patterns:
        suffix = re.search(suffix_pattern, name, re.IGNORECASE)
        if suffix and canonical_condominio21_unit(suffix.group(1).replace("Apto", "")) == (
            canonical_condominio21_unit(unit)
        ):
            name = name[:suffix.start()]
            break
    else:
        # Alguns relatórios unem o número do apartamento ao último sobrenome.
        if unit.isdigit():
            numeric_suffix = re.search(r"(\d+)$", name)
            if numeric_suffix and int(numeric_suffix.group(1)) == int(unit):
                name = name[:numeric_suffix.start()]
    return re.sub(r"[-\s]+$", "", name).strip()


@app.get("/")
def read_root():
    return {"status": "alive"}

@app.post("/v1/extract/condominio21", response_model=List[ChargeJSON])
async def extract_condominio21(file: UploadFile = File(...)):
    """Extrai Condomínio 21 somente quando toda a estrutura é reconhecida."""
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(
            status_code=422,
            detail={"error": "EMPTY_PDF", "message": "O PDF enviado está vazio ou corrompido."},
        )

    money = r"(?:-?\d+(?:\.\d{3})*,\d{2}|\(\d+(?:\.\d{3})*,\d{2}\))"
    charge_pattern = re.compile(
        rf"^(.+?)\s+(\d{{2}}/\d{{4}})\s+(\d{{2}}/\d{{2}}/\d{{4}})"
        rf"\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})$"
    )
    month_total_pattern = re.compile(
        rf"^Total\s+(\d{{2}}/\d{{4}}):\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})$",
        re.IGNORECASE,
    )
    labeled_total_pattern = re.compile(
        rf"^Total\s+'(.+?)':\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})$",
        re.IGNORECASE,
    )
    grand_total_pattern = re.compile(
        rf"^Total:\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})$",
        re.IGNORECASE,
    )
    total_general_pattern = re.compile(rf"^Total Geral:\s+({money})$", re.IGNORECASE)
    class_row_pattern = re.compile(rf"^(.+?)\s+({money})\s+({money})$")
    class_total_pattern = re.compile(rf"^({money})\s+({money})$")
    report_header_pattern = re.compile(
        r"^unidades inadimplentes (\d{2}/\d{2}/\d{4}) pag: (\d+)/(\d+)$"
    )
    unit_pattern = re.compile(
        r"^(?:\d{2,8}|[A-Za-z]{1,10}-\d{1,8}|[A-Za-z]{1,10}\d{1,8}|(?:Loja|Sala)\s+[A-Za-z0-9-]+)$",
        re.IGNORECASE,
    )
    expected_table_header = (
        "unidade mes ref vencimento valor juros multa correcao proj. rec."
    )
    expected_filters = (
        "mes: todos unidade: todas grupo/classe: todas cobranca: todas imobiliaria: todas"
    )
    zero_totals = lambda: [Decimal("0.00") for _ in range(5)]

    charges = []
    current_condominium = None
    current_unit = None
    current_name = None
    current_raw_name = None
    expecting_name = False
    current_unit_closed = True
    unit_totals = zero_totals()
    person_totals = zero_totals()
    month_totals = zero_totals()
    current_month = None
    month_total_seen = False
    grand_totals = zero_totals()
    unit_count = 0
    header_signature = None
    grand_total_seen = False
    reported_grand_total = None
    honor_amount = None
    total_general_seen = False
    class_summary_started = False
    class_summary_finished = False
    class_totals = [Decimal("0.00"), Decimal("0.00")]
    class_row_count = 0
    stats_stage = 0
    summary_complete = False

    try:
        pdf_context = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_PDF", "message": "O arquivo enviado está vazio ou corrompido."},
        )

    with pdf_context as pdf:
        if not pdf.pages:
            raise HTTPException(
                status_code=422,
                detail={"error": "EMPTY_PDF", "message": "O PDF enviado está vazio ou corrompido."},
            )

        page_count = len(pdf.pages)
        for page_index, page in enumerate(pdf.pages):
            page_number = page_index + 1
            text = page.extract_text(layout=True) or ""
            lines = [re.sub(r"\s+", " ", line.strip()) for line in text.splitlines() if line.strip()]
            if not lines:
                raise_condominio21_layout_error("página sem texto legível", page_number)

            footer = normalize_layout_text(lines[-1])
            if not re.match(
                r"^condominio21 corporate \(sql server\) - group software - \d+\.\d+/\d+(?:\.\d+)+$",
                footer,
            ):
                raise_condominio21_layout_error("rodapé ausente ou inválido", page_number)
            lines = lines[:-1]

            header_candidates = []
            for index, line in enumerate(lines[:2]):
                match = report_header_pattern.match(normalize_layout_text(line))
                if match:
                    header_candidates.append((index, match))
            if len(header_candidates) != 1:
                raise_condominio21_layout_error("cabeçalho principal ausente ou duplicado", page_number)

            header_index, header_match = header_candidates[0]
            if int(header_match.group(2)) != page_number or int(header_match.group(3)) != page_count:
                raise_condominio21_layout_error("paginação inconsistente", page_number)
            common_header_end = header_index + 6
            if len(lines) < common_header_end:
                raise_condominio21_layout_error("cabeçalho incompleto", page_number)

            page_header = lines[header_index:common_header_end]
            normalized_header = [normalize_layout_text(line) for line in page_header]
            if not current_condominium:
                current_condominium = page_header[1].strip()
                if not current_condominium:
                    raise_condominio21_layout_error("nome do condomínio ausente", page_number)
                if not normalized_header[2].startswith("inadimplencia ") or (
                    "para contas emitidas e sub judice" not in normalized_header[2]
                ):
                    raise_condominio21_layout_error("período de inadimplência inválido", page_number)
                if normalized_header[3] != expected_filters:
                    raise_condominio21_layout_error("filtros do relatório alterados", page_number)
                if not (
                    normalized_header[4].startswith("correcao:")
                    and " multa:" in normalized_header[4]
                    and re.search(r"data base: \d{2}/\d{2}/\d{4}$", normalized_header[4])
                ):
                    raise_condominio21_layout_error("parâmetros de correção inválidos", page_number)
                if not re.match(r"^juros: [\d.,]+% ao mes$", normalized_header[5]):
                    raise_condominio21_layout_error("parâmetro de juros inválido", page_number)
                header_signature = (
                    tuple(normalize_layout_text(line) for line in lines[:header_index]),
                    tuple(normalized_header[1:]),
                )
            else:
                page_signature = (
                    tuple(normalize_layout_text(line) for line in lines[:header_index]),
                    tuple(normalized_header[1:]),
                )
                if page_signature != header_signature:
                    raise_condominio21_layout_error("cabeçalho mudou entre as páginas", page_number)

            has_table_header = (
                len(lines) > common_header_end
                and normalize_layout_text(lines[common_header_end]) == expected_table_header
            )
            if has_table_header:
                header_end = common_header_end + 1
            elif (
                page_number == page_count
                and len(lines) > common_header_end
                and grand_total_pattern.match(lines[common_header_end])
            ):
                # Relatórios extensos podem reservar a última página apenas ao resumo.
                header_end = common_header_end
            else:
                raise_condominio21_layout_error("colunas da tabela alteradas ou ausentes", page_number)

            content_lines = lines[header_end:]
            for content_index, line in enumerate(content_lines):
                displayed_line_number = header_end + content_index + 1
                normalized_line = normalize_layout_text(line)

                if summary_complete:
                    raise_condominio21_layout_error(
                        "conteúdo encontrado depois do resumo final", page_number, displayed_line_number
                    )

                if grand_total_seen:
                    if normalized_line.startswith("honor") and ":" in line:
                        if honor_amount is not None or class_summary_started or stats_stage:
                            raise_condominio21_layout_error(
                                "honorários fora da posição esperada", page_number, displayed_line_number
                            )
                        honor_match = re.search(rf":\s*({money})$", line)
                        if not honor_match:
                            raise_condominio21_layout_error(
                                "valor de honorários inválido", page_number, displayed_line_number
                            )
                        honor_amount = parse_br_decimal(honor_match.group(1))
                        expected_honor = (reported_grand_total[4] * Decimal("0.10")).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        if honor_amount != expected_honor:
                            raise_condominio21_layout_error(
                                "honorários não conferem com o total projetado",
                                page_number,
                                displayed_line_number,
                            )
                        continue

                    total_general_match = total_general_pattern.match(line)
                    if total_general_match:
                        if honor_amount is None or total_general_seen or class_summary_started or stats_stage:
                            raise_condominio21_layout_error(
                                "total geral fora da posição esperada", page_number, displayed_line_number
                            )
                        total_general = parse_br_decimal(total_general_match.group(1))
                        if total_general != reported_grand_total[4] + honor_amount:
                            raise_condominio21_layout_error(
                                "total geral não confere", page_number, displayed_line_number
                            )
                        total_general_seen = True
                        continue

                    if normalized_line == "classe de conta total lancado total projetado":
                        if class_summary_started or stats_stage or (honor_amount is not None and not total_general_seen):
                            raise_condominio21_layout_error(
                                "resumo por classe fora da posição esperada",
                                page_number,
                                displayed_line_number,
                            )
                        class_summary_started = True
                        continue

                    class_total_match = class_total_pattern.match(line)
                    if class_total_match and class_summary_started and not class_summary_finished:
                        reported_class_totals = [
                            parse_br_decimal(class_total_match.group(1)),
                            parse_br_decimal(class_total_match.group(2)),
                        ]
                        if class_row_count == 0 or reported_class_totals != class_totals:
                            raise_condominio21_layout_error(
                                "total do resumo por classe não confere",
                                page_number,
                                displayed_line_number,
                            )
                        if reported_class_totals != [reported_grand_total[0], reported_grand_total[4]]:
                            raise_condominio21_layout_error(
                                "resumo por classe diverge do total do relatório",
                                page_number,
                                displayed_line_number,
                            )
                        class_summary_finished = True
                        continue

                    class_row_match = class_row_pattern.match(line)
                    if class_row_match and class_summary_started and not class_summary_finished:
                        values = [
                            parse_br_decimal(class_row_match.group(2)),
                            parse_br_decimal(class_row_match.group(3)),
                        ]
                        class_totals = [total + value for total, value in zip(class_totals, values)]
                        class_row_count += 1
                        continue

                    first_stat = re.match(r"^total de unidades inadimplentes (\d+)$", normalized_line)
                    second_stat = re.match(
                        r"^total unid\. inadimplentes \(separando vinculadas\) (\d+)$", normalized_line
                    )
                    third_stat = re.match(
                        r"^total de sacados / pessoas inadimplentes (\d+)$", normalized_line
                    )
                    if first_stat:
                        if stats_stage != 0 or (class_summary_started and not class_summary_finished):
                            raise_condominio21_layout_error(
                                "resumo de unidades fora da posição esperada",
                                page_number,
                                displayed_line_number,
                            )
                        if honor_amount is not None and not total_general_seen:
                            raise_condominio21_layout_error(
                                "total geral ausente", page_number, displayed_line_number
                            )
                        if int(first_stat.group(1)) != unit_count:
                            raise_condominio21_layout_error(
                                "quantidade de unidades não confere", page_number, displayed_line_number
                            )
                        stats_stage = 1
                        continue
                    if second_stat:
                        if stats_stage != 1 or int(second_stat.group(1)) != unit_count:
                            raise_condominio21_layout_error(
                                "quantidade de unidades vinculadas não confere",
                                page_number,
                                displayed_line_number,
                            )
                        stats_stage = 2
                        continue
                    if third_stat:
                        if stats_stage != 2:
                            raise_condominio21_layout_error(
                                "resumo de sacados fora da posição esperada",
                                page_number,
                                displayed_line_number,
                            )
                        stats_stage = 3
                        summary_complete = True
                        continue

                    raise_condominio21_layout_error(
                        "linha não reconhecida no resumo final", page_number, displayed_line_number
                    )

                charge_match = charge_pattern.match(line)
                if charge_match:
                    if current_unit_closed or expecting_name or not current_unit or not current_name:
                        raise_condominio21_layout_error(
                            "cobrança sem unidade e morador ativos", page_number, displayed_line_number
                        )
                    due_date = parse_br_date(charge_match.group(3))
                    if due_date == charge_match.group(3):
                        raise_condominio21_layout_error(
                            "data de vencimento inválida", page_number, displayed_line_number
                        )
                    amounts = [parse_br_decimal(charge_match.group(index)) for index in range(4, 9)]
                    if sum(amounts[:4], Decimal("0.00")) != amounts[4]:
                        raise_condominio21_layout_error(
                            "total da cobrança não confere", page_number, displayed_line_number
                        )

                    reference_month = charge_match.group(2)
                    if current_month != reference_month:
                        current_month = reference_month
                        month_totals = zero_totals()
                        month_total_seen = False
                    elif month_total_seen:
                        raise_condominio21_layout_error(
                            "cobrança encontrada depois do total do mês", page_number, displayed_line_number
                        )

                    month_totals = [total + value for total, value in zip(month_totals, amounts)]
                    person_totals = [total + value for total, value in zip(person_totals, amounts)]
                    unit_totals = [total + value for total, value in zip(unit_totals, amounts)]
                    grand_totals = [total + value for total, value in zip(grand_totals, amounts)]

                    original, interest, penalty, correction, projected = [float(value) for value in amounts]
                    legal_fees = round((original + interest + penalty + correction) * 0.1, 2)
                    charges.append(ChargeJSON(
                        extractCondominium=current_condominium,
                        unit=current_unit,
                        originalDraweeName=current_name,
                        description=charge_match.group(1).strip(),
                        referenceMonth=reference_month,
                        dueDate=due_date,
                        originalAmount=original,
                        interestAmount=interest,
                        penaltyAmount=penalty,
                        monetaryCorrection=correction,
                        legalFees=legal_fees,
                        totalAmount=round(projected + legal_fees, 2),
                    ))
                    continue

                month_total_match = month_total_pattern.match(line)
                if month_total_match:
                    if current_unit_closed or expecting_name or current_month != month_total_match.group(1):
                        raise_condominio21_layout_error(
                            "total mensal sem cobranças correspondentes", page_number, displayed_line_number
                        )
                    reported = [parse_br_decimal(month_total_match.group(index)) for index in range(2, 7)]
                    if month_total_seen or reported != month_totals:
                        raise_condominio21_layout_error(
                            "total mensal não confere", page_number, displayed_line_number
                        )
                    month_total_seen = True
                    continue

                labeled_total_match = labeled_total_pattern.match(line)
                if labeled_total_match:
                    if current_unit_closed or not current_unit or not current_name:
                        raise_condominio21_layout_error(
                            "totalizador sem unidade ativa", page_number, displayed_line_number
                        )
                    label = labeled_total_match.group(1).strip()
                    reported = [parse_br_decimal(labeled_total_match.group(index)) for index in range(2, 7)]
                    if canonical_condominio21_unit(label) == canonical_condominio21_unit(current_unit):
                        if reported != unit_totals:
                            raise_condominio21_layout_error(
                                "total da unidade não confere", page_number, displayed_line_number
                            )
                        current_unit_closed = True
                        expecting_name = False
                    elif normalize_layout_text(label) == normalize_layout_text(current_raw_name):
                        if expecting_name or reported != person_totals:
                            raise_condominio21_layout_error(
                                "total do morador não confere", page_number, displayed_line_number
                            )
                        expecting_name = True
                        person_totals = zero_totals()
                        current_month = None
                        month_totals = zero_totals()
                        month_total_seen = False
                    else:
                        raise_condominio21_layout_error(
                            "totalizador pertence a uma unidade ou morador desconhecido",
                            page_number,
                            displayed_line_number,
                        )
                    continue

                grand_total_match = grand_total_pattern.match(line)
                if grand_total_match:
                    if grand_total_seen or not current_unit_closed or unit_count == 0:
                        raise_condominio21_layout_error(
                            "total geral das cobranças fora da posição esperada",
                            page_number,
                            displayed_line_number,
                        )
                    reported_grand_total = [
                        parse_br_decimal(grand_total_match.group(index)) for index in range(1, 6)
                    ]
                    if reported_grand_total != grand_totals:
                        raise_condominio21_layout_error(
                            "total do relatório não confere com as cobranças",
                            page_number,
                            displayed_line_number,
                        )
                    grand_total_seen = True
                    continue

                if expecting_name:
                    clean_name = clean_condominio21_name(line, current_unit)
                    if len(clean_name) < 3 or not any(character.isalpha() for character in clean_name):
                        raise_condominio21_layout_error(
                            "nome do morador ausente ou inválido", page_number, displayed_line_number
                        )
                    current_name = clean_name
                    current_raw_name = line.strip()
                    expecting_name = False
                    person_totals = zero_totals()
                    current_month = None
                    month_totals = zero_totals()
                    month_total_seen = False
                    continue

                if unit_pattern.match(line):
                    if not current_unit_closed:
                        raise_condominio21_layout_error(
                            "nova unidade encontrada antes do total da anterior",
                            page_number,
                            displayed_line_number,
                        )
                    current_unit = line.strip()
                    current_name = None
                    current_raw_name = None
                    expecting_name = True
                    current_unit_closed = False
                    unit_totals = zero_totals()
                    person_totals = zero_totals()
                    current_month = None
                    month_totals = zero_totals()
                    month_total_seen = False
                    unit_count += 1
                    continue

                raise_condominio21_layout_error(
                    "linha não reconhecida pelo layout homologado", page_number, displayed_line_number
                )

    if not charges or not current_condominium or unit_count == 0:
        raise_condominio21_layout_error("nenhuma cobrança válida encontrada")
    if not current_unit_closed:
        raise_condominio21_layout_error("última unidade sem totalizador")
    if not grand_total_seen:
        raise_condominio21_layout_error("total do relatório ausente")
    if not summary_complete or stats_stage != 3:
        raise_condominio21_layout_error("resumo final incompleto")

    return charges

@app.post("/v1/extract/superlogica", response_model=List[ChargeJSON])
async def extract_superlogica(file: UploadFile = File(...)):
    """Extrai o padrão Superlógica somente quando toda a estrutura é reconhecida."""
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "EMPTY_PDF", "message": "O PDF enviado está vazio ou corrompido."},
        )

    money = r"(?:-?\d+(?:\.\d{3})*,\d{2}|\(\d+(?:\.\d{3})*,\d{2}\))"
    charge_pattern = re.compile(
        rf"^(\d{{2}}/\d{{2}}/\d{{2,4}})\s+(\d{{2}}/\d{{4}})\s+(\d+)\s+(\d+)"
        rf"\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})$"
    )
    total_pattern = re.compile(
        rf"^Total\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})$",
        re.IGNORECASE,
    )
    summary_pattern = re.compile(
        rf"^(\d+)\s+unidades?\s+inadimplentes?\s+\((\d{{1,3}}(?:,\d{{2}})?)%\)\s+({money})\s+({money})$",
        re.IGNORECASE,
    )
    condo_pattern = re.compile(r"^[A-Za-z0-9]+\s+(.+?)\s+\((\d+)\)$")
    updated_pattern = re.compile(r"^Valores atualizados até \d{2}/\d{2}/\d{4}$", re.IGNORECASE)
    legacy_unit_pattern = re.compile(
        r"^([A-Za-z0-9-]{1,10})\s+Unidade\s+(?:[-–—]\s*)?(.+)$", re.IGNORECASE
    )
    block_unit_pattern = re.compile(
        r"^([A-Za-z0-9-]{1,10})\s+BLOCO\s+([A-Za-z0-9-]{1,10})\s+(?:[-–—]\s*)?(.+)$",
        re.IGNORECASE,
    )
    expected_table_header = (
        "vencimento compet. atraso codigo principal juros multa atualiz. honorarios total"
    )

    charges = []
    current_unit = None
    current_name = None
    current_unit_closed = True
    table_header_seen = False
    current_unit_totals = [Decimal("0.00") for _ in range(6)]
    current_unit_charge_count = 0
    current_condominium = None
    unit_count = 0
    summary = None
    grand_totals = [Decimal("0.00") for _ in range(6)]

    try:
        pdf_context = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_PDF", "message": "O arquivo enviado está vazio ou corrompido."},
        )

    with pdf_context as pdf:
        if not pdf.pages:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "EMPTY_PDF", "message": "O PDF enviado está vazio ou corrompido."},
            )

        page_count = len(pdf.pages)

        for page_index, page in enumerate(pdf.pages):
            page_number = page_index + 1
            text = page.extract_text(layout=True) or ""
            lines = [re.sub(r"\s+", " ", line.strip()) for line in text.splitlines() if line.strip()]
            if not lines:
                raise_superlogica_layout_error("página sem texto legível", page_number)

            # O contador de páginas delimita o rodapé. Nada do rodapé participa da extração.
            page_markers = []
            for index, line in enumerate(lines):
                marker = re.search(r"(?:^|\s)(\d+)\s+de\s+(\d+)$", line, re.IGNORECASE)
                if marker:
                    page_markers.append((index, marker))

            if len(page_markers) != 1:
                raise_superlogica_layout_error("contador de página ausente ou duplicado", page_number)

            marker_index, marker = page_markers[0]
            if int(marker.group(1)) != page_number or int(marker.group(2)) != page_count:
                raise_superlogica_layout_error("contador de página inconsistente", page_number)

            marker_is_standalone = marker.group(0).strip() == lines[marker_index]
            footer_start = marker_index - 3 if marker_is_standalone else marker_index
            expected_footer_size = 4 if marker_is_standalone else 3
            if footer_start < 0 or len(lines) - footer_start != expected_footer_size:
                raise_superlogica_layout_error("rodapé fora da estrutura esperada", page_number)
            lines = lines[:footer_start]

            if page_number == 1:
                if len(lines) < 3:
                    raise_superlogica_layout_error("cabeçalho incompleto", page_number)
                condo_match = condo_pattern.match(lines[0])
                if not condo_match:
                    raise_superlogica_layout_error("identificação do condomínio inválida", page_number, 1)
                if normalize_layout_text(lines[1]) != "inadimplentes":
                    raise_superlogica_layout_error("título do relatório inválido", page_number, 2)
                if not updated_pattern.match(lines[2]):
                    raise_superlogica_layout_error("data de atualização inválida", page_number, 3)
                current_condominium = condo_match.group(1).strip()
                lines = lines[3:]

            for line_index, line in enumerate(lines):
                displayed_line_number = line_index + 1
                if summary is not None:
                    raise_superlogica_layout_error(
                        "conteúdo encontrado depois do resumo final",
                        page_number,
                        displayed_line_number,
                    )
                normalized_line = normalize_layout_text(line)
                next_line = lines[line_index + 1] if line_index + 1 < len(lines) else ""
                next_is_table_header = (
                    normalize_layout_text(next_line) == expected_table_header
                )

                if next_is_table_header:
                    if not current_unit_closed:
                        raise_superlogica_layout_error(
                            "nova unidade encontrada antes do total da unidade anterior",
                            page_number,
                            displayed_line_number,
                        )

                    legacy_match = legacy_unit_pattern.match(line)
                    block_match = block_unit_pattern.match(line)
                    if legacy_match:
                        unit = legacy_match.group(1).strip()
                        raw_name = legacy_match.group(2)
                    elif block_match:
                        unit = f"{block_match.group(2).strip()}-{block_match.group(1).strip()}"
                        raw_name = block_match.group(3)
                    else:
                        raise_superlogica_layout_error(
                            "formato da unidade ou do morador não reconhecido",
                            page_number,
                            displayed_line_number,
                        )

                    name = clean_superlogica_name(raw_name)
                    if len(name) < 3 or not any(character.isalpha() for character in name):
                        raise_superlogica_layout_error(
                            "nome do morador ausente ou inválido", page_number, displayed_line_number
                        )
                    if re.match(r"^[-–—]", name):
                        raise_superlogica_layout_error(
                            "separador inesperado no início do nome do morador",
                            page_number,
                            displayed_line_number,
                        )

                    current_unit = unit
                    current_name = name
                    current_unit_closed = False
                    current_unit_totals = [Decimal("0.00") for _ in range(6)]
                    current_unit_charge_count = 0
                    unit_count += 1
                    continue

                if normalized_line == expected_table_header:
                    if not current_unit or current_unit_closed:
                        raise_superlogica_layout_error(
                            "cabeçalho da tabela sem unidade ativa", page_number, displayed_line_number
                        )
                    if table_header_seen:
                        # Algumas versões repetem as colunas ao continuar uma unidade
                        # na página seguinte; outras começam diretamente pela cobrança.
                        if line_index != 0:
                            raise_superlogica_layout_error(
                                "cabeçalho da tabela duplicado", page_number, displayed_line_number
                            )
                    table_header_seen = True
                    continue

                charge_match = charge_pattern.match(line)
                if charge_match:
                    if not current_unit or current_unit_closed or not table_header_seen:
                        raise_superlogica_layout_error(
                            "cobrança fora de uma tabela reconhecida", page_number, displayed_line_number
                        )
                    if parse_br_date(charge_match.group(1)) == charge_match.group(1):
                        raise_superlogica_layout_error(
                            "data de vencimento inválida", page_number, displayed_line_number
                        )

                    amounts = [parse_br_decimal(charge_match.group(index)) for index in range(5, 11)]
                    current_unit_totals = [
                        accumulated + amount
                        for accumulated, amount in zip(current_unit_totals, amounts)
                    ]
                    grand_totals = [
                        accumulated + amount for accumulated, amount in zip(grand_totals, amounts)
                    ]
                    current_unit_charge_count += 1

                    charges.append(ChargeJSON(
                        extractCondominium=current_condominium,
                        unit=current_unit,
                        originalDraweeName=current_name,
                        dueDate=parse_br_date(charge_match.group(1)),
                        referenceMonth=charge_match.group(2),
                        documentNumber=charge_match.group(4),
                        originalAmount=float(amounts[0]),
                        interestAmount=float(amounts[1]),
                        penaltyAmount=float(amounts[2]),
                        monetaryCorrection=float(amounts[3]),
                        legalFees=float(amounts[4]),
                        totalAmount=float(amounts[5]),
                    ))
                    continue

                total_match = total_pattern.match(line)
                if total_match:
                    if current_unit_closed or not table_header_seen or current_unit_charge_count == 0:
                        raise_superlogica_layout_error(
                            "totalizador sem cobranças reconhecidas", page_number, displayed_line_number
                        )
                    reported_totals = [
                        parse_br_decimal(total_match.group(index)) for index in range(1, 7)
                    ]
                    if reported_totals != current_unit_totals:
                        raise_superlogica_layout_error(
                            "valores do totalizador não conferem com as cobranças",
                            page_number,
                            displayed_line_number,
                        )
                    current_unit_closed = True
                    table_header_seen = False
                    continue

                summary_match = summary_pattern.match(line)
                if summary_match:
                    if summary is not None:
                        raise_superlogica_layout_error(
                            "resumo final duplicado", page_number, displayed_line_number
                        )
                    if not current_unit_closed:
                        raise_superlogica_layout_error(
                            "resumo encontrado antes do total da última unidade",
                            page_number,
                            displayed_line_number,
                        )
                    summary = (
                        int(summary_match.group(1)),
                        parse_br_decimal(summary_match.group(3)),
                        parse_br_decimal(summary_match.group(4)),
                    )
                    continue

                raise_superlogica_layout_error(
                    "linha não reconhecida pelo layout homologado", page_number, displayed_line_number
                )

    if not charges or not current_condominium or unit_count == 0:
        raise_superlogica_layout_error("nenhuma cobrança válida encontrada")
    if not current_unit_closed:
        raise_superlogica_layout_error("última unidade sem totalizador")
    if summary is None:
        raise_superlogica_layout_error("resumo final ausente")
    if summary[0] != unit_count:
        raise_superlogica_layout_error("quantidade de unidades do resumo não confere")
    if summary[1] != grand_totals[0] or summary[2] != grand_totals[5]:
        raise_superlogica_layout_error("valores do resumo final não conferem com as cobranças")

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
    """Extrai UCondo somente quando toda a estrutura é reconhecida."""
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(
            status_code=422,
            detail={"error": "EMPTY_PDF", "message": "O PDF enviado está vazio ou corrompido."},
        )

    money = r"(?:-?\d+(?:\.\d{3})*,\d{2}|\(\d+(?:\.\d{3})*,\d{2}\))"
    charge_pattern = re.compile(
        rf"^(.+?)\s+(\d{{2}}/\d{{2}}/\d{{4}})"
        rf"\s+R\$\s*({money})\s+R\$\s*({money})\s+R\$\s*({money})"
        rf"\s+R\$\s*({money})\s+R\$\s*({money})\s+R\$\s*({money})$"
    )
    six_amounts_pattern = re.compile(
        rf"^R\$\s*({money})\s+R\$\s*({money})\s+R\$\s*({money})"
        rf"\s+R\$\s*({money})\s+R\$\s*({money})\s+R\$\s*({money})$"
    )
    summary_pattern = re.compile(
        rf"^(\d+)\s+(\d+)\s+R\$\s*({money})\s+R\$\s*({money})"
        rf"\s+R\$\s*({money})\s+R\$\s*({money})\s+R\$\s*({money})"
        rf"\s+R\$\s*({money})$"
    )
    unit_pattern = re.compile(r"^Bloco\s+[A-Za-z0-9-]+\s+apto\s+[A-Za-z0-9-]+$")
    party_pattern = re.compile(
        r"^(.+?)\s+(\d{3}\.\d{3}\.\d{3}-\d{2}|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})"
        r"\s+(\(\d{2}\)\s*\d{4,5}-\d{4})\s+(\S+@\S+)$"
    )
    footer_pattern = re.compile(
        r"^gerado em (\d{2}/\d{2}/\d{4}) (\d{2}:\d{2}) (.+?) pagina (\d+)\s*de\s*(\d+)$"
    )
    period_pattern = re.compile(
        r"^periodo \d{2}/\d{2}/\d{4} \S+ \d{2}/\d{2}/\d{4}$"
    )
    expected_table_header = (
        "pendencia vencimento valor original correcao multa juros honorarios total"
    )
    expected_summary_header = (
        "unidades com atrasos quantidade de atrasos valor original correcao "
        "multa juros honorarios total"
    )
    zero_totals = lambda: [Decimal("0.00") for _ in range(6)]

    charges = []
    current_condominium = None
    header_signature = None
    footer_signature = None
    current_unit = None
    current_name = None
    current_document = None
    current_unit_closed = True
    party_seen = False
    table_header_seen = False
    current_unit_charge_count = 0
    current_unit_totals = zero_totals()
    grand_totals = zero_totals()
    unit_count = 0
    seen_units = set()
    summary_stage = 0

    try:
        pdf_context = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_PDF", "message": "O arquivo enviado está vazio ou corrompido."},
        )

    with pdf_context as pdf:
        if not pdf.pages:
            raise HTTPException(
                status_code=422,
                detail={"error": "EMPTY_PDF", "message": "O PDF enviado está vazio ou corrompido."},
            )

        page_count = len(pdf.pages)
        for page_index, page in enumerate(pdf.pages):
            page_number = page_index + 1
            text = page.extract_text(layout=True) or ""
            lines = [re.sub(r"\s+", " ", line.strip()) for line in text.splitlines() if line.strip()]
            if len(lines) < 5:
                raise_ucondo_layout_error("página sem a estrutura mínima esperada", page_number)

            normalized_title = normalize_layout_text(lines[0])
            normalized_condominium = normalize_layout_text(lines[1])
            normalized_period = normalize_layout_text(lines[2])
            if normalized_title != "relatorio de inadimplentes":
                raise_ucondo_layout_error("título do relatório ausente ou inválido", page_number, 1)
            if not normalized_condominium or normalized_condominium.startswith("periodo"):
                raise_ucondo_layout_error("nome do condomínio ausente", page_number, 2)
            if not period_pattern.fullmatch(normalized_period):
                raise_ucondo_layout_error("período do relatório ausente ou inválido", page_number, 3)

            page_header_signature = (normalized_condominium, normalized_period)
            if header_signature is None:
                header_signature = page_header_signature
                current_condominium = lines[1]
            elif page_header_signature != header_signature:
                raise_ucondo_layout_error("cabeçalho divergente entre as páginas", page_number)

            normalized_footer = normalize_layout_text(lines[-1])
            footer_match = footer_pattern.fullmatch(normalized_footer)
            if not footer_match:
                raise_ucondo_layout_error("rodapé ausente ou inválido", page_number, len(lines))
            if int(footer_match.group(4)) != page_number or int(footer_match.group(5)) != page_count:
                raise_ucondo_layout_error("paginação do rodapé não confere", page_number, len(lines))
            page_footer_signature = footer_match.group(1, 2, 3)
            if footer_signature is None:
                footer_signature = page_footer_signature
            elif page_footer_signature != footer_signature:
                raise_ucondo_layout_error("rodapé divergente entre as páginas", page_number)

            for displayed_line_number, line in enumerate(lines[3:-1], start=4):
                normalized_line = normalize_layout_text(line)

                if summary_stage:
                    if summary_stage == 1 and normalized_line == expected_summary_header:
                        summary_stage = 2
                        continue
                    if summary_stage == 2:
                        summary_match = summary_pattern.fullmatch(line)
                        if not summary_match:
                            raise_ucondo_layout_error(
                                "totalização geral ausente ou inválida", page_number, displayed_line_number
                            )
                        reported_units = int(summary_match.group(1))
                        reported_charges = int(summary_match.group(2))
                        reported_totals = [parse_br_decimal(summary_match.group(i)) for i in range(3, 9)]
                        if reported_units != unit_count or reported_charges != len(charges):
                            raise_ucondo_layout_error(
                                "quantidades da totalização geral não conferem",
                                page_number,
                                displayed_line_number,
                            )
                        if reported_totals != grand_totals:
                            raise_ucondo_layout_error(
                                "valores da totalização geral não conferem",
                                page_number,
                                displayed_line_number,
                            )
                        summary_stage = 3
                        continue
                    raise_ucondo_layout_error(
                        "conteúdo inesperado após a totalização geral", page_number, displayed_line_number
                    )

                if normalized_line == "totalizacoes gerais":
                    if not current_unit_closed or not charges:
                        raise_ucondo_layout_error(
                            "totalização geral encontrada antes do fechamento das unidades",
                            page_number,
                            displayed_line_number,
                        )
                    summary_stage = 1
                    continue

                if unit_pattern.fullmatch(line):
                    if not current_unit_closed:
                        raise_ucondo_layout_error(
                            "nova unidade encontrada antes do totalizador da unidade anterior",
                            page_number,
                            displayed_line_number,
                        )
                    if line in seen_units:
                        raise_ucondo_layout_error(
                            "unidade repetida no relatório", page_number, displayed_line_number
                        )
                    seen_units.add(line)
                    current_unit = line
                    current_name = None
                    current_document = None
                    current_unit_closed = False
                    party_seen = False
                    table_header_seen = False
                    current_unit_charge_count = 0
                    current_unit_totals = zero_totals()
                    unit_count += 1
                    continue

                party_match = party_pattern.fullmatch(line)
                if party_match:
                    if current_unit_closed or party_seen or table_header_seen:
                        raise_ucondo_layout_error(
                            "responsável fora da posição esperada", page_number, displayed_line_number
                        )
                    current_name = party_match.group(1).strip()
                    current_document = party_match.group(2)
                    if not current_name:
                        raise_ucondo_layout_error(
                            "nome do responsável ausente", page_number, displayed_line_number
                        )
                    party_seen = True
                    continue

                if normalized_line == expected_table_header:
                    if current_unit_closed or not party_seen or table_header_seen:
                        raise_ucondo_layout_error(
                            "cabeçalho da tabela fora da posição esperada",
                            page_number,
                            displayed_line_number,
                        )
                    table_header_seen = True
                    continue

                charge_match = charge_pattern.fullmatch(line)
                if charge_match:
                    if current_unit_closed or not party_seen or not table_header_seen:
                        raise_ucondo_layout_error(
                            "pendência encontrada antes dos dados obrigatórios da unidade",
                            page_number,
                            displayed_line_number,
                        )
                    try:
                        datetime.strptime(charge_match.group(2), "%d/%m/%Y")
                    except ValueError:
                        raise_ucondo_layout_error(
                            "data de vencimento inválida", page_number, displayed_line_number
                        )
                    amounts = [parse_br_decimal(charge_match.group(i)) for i in range(3, 9)]
                    if sum(amounts[:5], Decimal("0.00")) != amounts[5]:
                        raise_ucondo_layout_error(
                            "total da pendência não confere com suas parcelas",
                            page_number,
                            displayed_line_number,
                        )
                    description = charge_match.group(1).strip()
                    charges.append(
                        ChargeJSON(
                            extractCondominium=current_condominium,
                            unit=current_unit,
                            originalDraweeName=current_name,
                            originalDraweeDocument=current_document,
                            description=description,
                            referenceMonth=extract_ref_month(description),
                            dueDate=parse_br_date(charge_match.group(2)),
                            originalAmount=float(amounts[0]),
                            monetaryCorrection=float(amounts[1]),
                            penaltyAmount=float(amounts[2]),
                            interestAmount=float(amounts[3]),
                            legalFees=float(amounts[4]),
                            totalAmount=float(amounts[5]),
                        )
                    )
                    current_unit_charge_count += 1
                    current_unit_totals = [
                        current_unit_totals[index] + amounts[index] for index in range(6)
                    ]
                    grand_totals = [grand_totals[index] + amounts[index] for index in range(6)]
                    continue

                unit_total_match = six_amounts_pattern.fullmatch(line)
                if unit_total_match:
                    if current_unit_closed or not table_header_seen or current_unit_charge_count == 0:
                        raise_ucondo_layout_error(
                            "totalizador de unidade fora da posição esperada",
                            page_number,
                            displayed_line_number,
                        )
                    reported_totals = [
                        parse_br_decimal(unit_total_match.group(index)) for index in range(1, 7)
                    ]
                    if reported_totals != current_unit_totals:
                        raise_ucondo_layout_error(
                            "totalizador da unidade não confere com as pendências",
                            page_number,
                            displayed_line_number,
                        )
                    current_unit_closed = True
                    current_unit = None
                    current_name = None
                    current_document = None
                    continue

                raise_ucondo_layout_error(
                    "linha não reconhecida pelo layout homologado", page_number, displayed_line_number
                )

    if not charges or unit_count == 0 or not current_condominium:
        raise_ucondo_layout_error("nenhuma pendência válida encontrada")
    if not current_unit_closed:
        raise_ucondo_layout_error("última unidade sem totalizador")
    if summary_stage != 3:
        raise_ucondo_layout_error("totalização geral ausente ou incompleta")

    return charges


@app.post("/v1/extract/condomob", response_model=List[ChargeJSON])
async def extract_condomob(file: UploadFile = File(...)):
    """Extrai Condomob somente quando toda a estrutura é reconhecida."""
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(
            status_code=422,
            detail={"error": "EMPTY_PDF", "message": "O PDF enviado está vazio ou corrompido."},
        )

    money = r"(?:-?\d+(?:\.\d{3})*,\d{2}|\(\d+(?:\.\d{3})*,\d{2}\))"
    row_end_pattern = re.compile(
        rf"(?:(\d{{2}}/\d{{4}})\s+(\d{{2}}/\d{{2}}/\d{{4}})\s+(\d+)"
        rf"|(\d{{2}}/\d{{2}}/\d{{4}})\s+(\d+)\s+(\d{{2}}/\d{{4}}))"
        rf"\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})$"
    )
    row_prefix_pattern = re.compile(
        r"^(\S+)\s+(.+?)\s+(?:(\d+/\d+)\s+)?(\d+)(?:\s+(\d+))?$"
    )
    combined_unit_pattern = re.compile(
        r"^([A-Za-z0-9*-]+)\s+Pagador:\s*(Propriet.rio|Inquilino)$", re.IGNORECASE
    )
    split_payer_pattern = re.compile(r"^Pagador:\s*(Propriet.rio|Inquilino)$", re.IGNORECASE)
    standalone_unit_pattern = re.compile(r"^[A-Za-z0-9*-]+$")
    party_entry_pattern = re.compile(
        r"(Propriet.rio|Inquilino):\s+(.+?)\s+\(([\d./-]*)\)"
        r"(?=\s+(?:Propriet.rio|Inquilino):|$)",
        re.IGNORECASE,
    )
    unit_total_pattern = re.compile(
        rf"^([A-Za-z0-9*-]+):\s+(\d+)\s+cobrança\(s\)"
        rf"\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})\s+({money})$",
        re.IGNORECASE,
    )
    page_header_pattern = re.compile(r"^inadimplencia pag\. (\d+) de\s*(\d+)$")
    final_summary_pattern = re.compile(
        rf"^(\d+)\s+unidade\(s\)\s+\(([\d.,]+)%\s+de\s+(\d+)\)"
        rf"\s+(\d+)\s+cobranca\(s\)\s+({money})\s+({money})\s+({money})"
        rf"\s+({money})\s+({money})\s+({money})$"
    )
    expected_table_header = (
        "tipo pagador parc doc n. numero periodo vencimento dias "
        "valor multa juros atual. hon. vl.atual."
    )
    expected_total_header = "total valor multa juros atual. hon. vl.atual."
    allowed_charge_types = {"ordinaria", "acordo", "extra"}
    zero_totals = lambda: [Decimal("0.00") for _ in range(6)]

    charges = []
    current_condominium = None
    header_signature = None
    footer_signature = None
    current_unit_token = None
    current_unit = None
    current_payer = None
    pending_payer = None
    current_name = None
    current_document = None
    current_owner_name = None
    current_owner_document = None
    current_tenant_name = None
    current_tenant_document = None
    parties_seen = False
    table_header_seen = False
    current_unit_closed = True
    current_unit_charge_count = 0
    current_unit_totals = zero_totals()
    grand_totals = zero_totals()
    unit_count = 0
    summary_header_seen = False
    summary_complete = False

    try:
        pdf_context = pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_PDF", "message": "O arquivo enviado está vazio ou corrompido."},
        )

    with pdf_context as pdf:
        if not pdf.pages:
            raise HTTPException(
                status_code=422,
                detail={"error": "EMPTY_PDF", "message": "O PDF enviado está vazio ou corrompido."},
            )

        page_count = len(pdf.pages)
        for page_index, page in enumerate(pdf.pages):
            page_number = page_index + 1
            text = page.extract_text(layout=True) or ""
            lines = [re.sub(r"\s+", " ", line.strip()) for line in text.splitlines() if line.strip()]
            if not lines:
                raise_condomob_layout_error("página sem texto legível", page_number)

            normalized_footer = normalize_layout_text(lines[-1])
            if not re.match(
                r"^torre assessoria condominial \d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$",
                normalized_footer,
            ):
                raise_condomob_layout_error("rodapé ausente ou inválido", page_number)
            if footer_signature is None:
                footer_signature = normalized_footer
            elif normalized_footer != footer_signature:
                raise_condomob_layout_error("rodapé mudou entre as páginas", page_number)
            lines = lines[:-1]

            if page_number == 1:
                if len(lines) < 5:
                    raise_condomob_layout_error("cabeçalho incompleto", page_number)
                title_match = re.match(r"^(.+?)\s+Inadimpl.ncia$", lines[0], re.IGNORECASE)
                if not title_match:
                    raise_condomob_layout_error("nome do condomínio inválido", page_number, 1)
                current_condominium = title_match.group(1).strip()
                if normalize_layout_text(lines[1]) != "palmas - to":
                    raise_condomob_layout_error("localidade do relatório alterada", page_number, 2)
                page_header_index = 2
                content_start = 5
            else:
                page_header_index = 0
                content_start = 3

            page_header_match = page_header_pattern.match(
                normalize_layout_text(lines[page_header_index])
            )
            if not page_header_match:
                raise_condomob_layout_error("paginação ausente ou inválida", page_number)
            if (
                int(page_header_match.group(1)) != page_number
                or int(page_header_match.group(2)) != page_count
            ):
                raise_condomob_layout_error("paginação inconsistente", page_number)
            if len(lines) < content_start:
                raise_condomob_layout_error("cabeçalho incompleto", page_number)

            reference_line = normalize_layout_text(lines[page_header_index + 1])
            rates_line = normalize_layout_text(lines[page_header_index + 2])
            if not (
                re.match(r"^data de referencia: \d{2}/\d{2}/\d{4};", reference_line)
                and "unidades com/sem processo judicial" in reference_line
                and "valor atualizado" in reference_line
            ):
                raise_condomob_layout_error("parâmetros de referência alterados", page_number)
            if not (
                rates_line.startswith("multa:")
                and "; juros:" in rates_line
                and "; indice de atualizacao:" in rates_line
            ):
                raise_condomob_layout_error("parâmetros financeiros alterados", page_number)

            page_signature = (reference_line, rates_line)
            if header_signature is None:
                header_signature = page_signature
            elif page_signature != header_signature:
                raise_condomob_layout_error("cabeçalho mudou entre as páginas", page_number)

            for content_index, line in enumerate(lines[content_start:]):
                displayed_line_number = content_start + content_index + 1
                normalized_line = normalize_layout_text(line)

                if summary_complete:
                    raise_condomob_layout_error(
                        "conteúdo encontrado depois do resumo final", page_number, displayed_line_number
                    )

                if normalized_line == expected_total_header:
                    if summary_header_seen or not current_unit_closed or pending_payer:
                        raise_condomob_layout_error(
                            "cabeçalho do resumo fora da posição esperada",
                            page_number,
                            displayed_line_number,
                        )
                    summary_header_seen = True
                    continue

                summary_match = final_summary_pattern.match(normalized_line)
                if summary_match:
                    if not summary_header_seen or not current_unit_closed:
                        raise_condomob_layout_error(
                            "resumo final fora da posição esperada", page_number, displayed_line_number
                        )
                    reported_unit_count = int(summary_match.group(1))
                    reported_charge_count = int(summary_match.group(4))
                    reported_totals = [
                        parse_br_decimal(summary_match.group(index)) for index in range(5, 11)
                    ]
                    if reported_unit_count != unit_count:
                        raise_condomob_layout_error(
                            "quantidade de unidades não confere", page_number, displayed_line_number
                        )
                    if reported_charge_count != len(charges):
                        raise_condomob_layout_error(
                            "quantidade de cobranças não confere", page_number, displayed_line_number
                        )
                    if reported_totals != grand_totals:
                        raise_condomob_layout_error(
                            "totais do resumo não conferem", page_number, displayed_line_number
                        )
                    summary_complete = True
                    continue

                if summary_header_seen:
                    raise_condomob_layout_error(
                        "linha inválida depois do cabeçalho do resumo",
                        page_number,
                        displayed_line_number,
                    )

                combined_unit_match = combined_unit_pattern.match(line)
                if combined_unit_match:
                    if not current_unit_closed or pending_payer:
                        raise_condomob_layout_error(
                            "nova unidade encontrada antes do total da anterior",
                            page_number,
                            displayed_line_number,
                        )
                    current_unit_token = combined_unit_match.group(1)
                    current_unit = current_unit_token.replace("*", "")
                    current_payer = normalize_layout_text(combined_unit_match.group(2))
                    current_unit_closed = False
                    current_unit_charge_count = 0
                    current_unit_totals = zero_totals()
                    current_name = None
                    current_document = None
                    current_owner_name = None
                    current_owner_document = None
                    current_tenant_name = None
                    current_tenant_document = None
                    parties_seen = False
                    table_header_seen = False
                    unit_count += 1
                    continue

                split_payer_match = split_payer_pattern.match(line)
                if split_payer_match:
                    if not current_unit_closed or pending_payer:
                        raise_condomob_layout_error(
                            "pagador fora da posição esperada", page_number, displayed_line_number
                        )
                    pending_payer = normalize_layout_text(split_payer_match.group(1))
                    continue

                if pending_payer and standalone_unit_pattern.match(line):
                    current_unit_token = line
                    current_unit = line.replace("*", "")
                    current_payer = pending_payer
                    pending_payer = None
                    current_unit_closed = False
                    current_unit_charge_count = 0
                    current_unit_totals = zero_totals()
                    current_name = None
                    current_document = None
                    current_owner_name = None
                    current_owner_document = None
                    current_tenant_name = None
                    current_tenant_document = None
                    parties_seen = False
                    table_header_seen = False
                    unit_count += 1
                    continue

                party_matches = list(party_entry_pattern.finditer(line))
                if party_matches:
                    cursor = 0
                    for party_match in party_matches:
                        if line[cursor:party_match.start()].strip():
                            raise_condomob_layout_error(
                                "identificação das partes contém texto inesperado",
                                page_number,
                                displayed_line_number,
                            )
                        cursor = party_match.end()
                    if line[cursor:].strip() or current_unit_closed or pending_payer or table_header_seen:
                        raise_condomob_layout_error(
                            "identificação das partes fora da posição esperada",
                            page_number,
                            displayed_line_number,
                        )
                    for party_match in party_matches:
                        role = normalize_layout_text(party_match.group(1))
                        name = party_match.group(2).strip()
                        document = format_document(party_match.group(3))
                        if role == "proprietario":
                            if current_owner_name:
                                raise_condomob_layout_error(
                                    "proprietário duplicado", page_number, displayed_line_number
                                )
                            current_owner_name = name
                            current_owner_document = document
                        else:
                            if current_tenant_name:
                                raise_condomob_layout_error(
                                    "inquilino duplicado", page_number, displayed_line_number
                                )
                            current_tenant_name = name
                            current_tenant_document = document
                    parties_seen = True
                    continue

                if normalized_line == expected_table_header:
                    if current_unit_closed or not parties_seen or table_header_seen:
                        raise_condomob_layout_error(
                            "cabeçalho da tabela fora da posição esperada",
                            page_number,
                            displayed_line_number,
                        )
                    if current_payer == "inquilino":
                        current_name = current_tenant_name
                        current_document = current_tenant_document
                    else:
                        current_name = current_owner_name
                        current_document = current_owner_document
                    if not current_name or not current_document:
                        raise_condomob_layout_error(
                            "nome ou documento do pagador inválido", page_number, displayed_line_number
                        )
                    table_header_seen = True
                    continue

                unit_total_match = unit_total_pattern.match(line)
                if unit_total_match:
                    if current_unit_closed or not table_header_seen or current_unit_charge_count == 0:
                        raise_condomob_layout_error(
                            "totalizador sem cobranças reconhecidas", page_number, displayed_line_number
                        )
                    if unit_total_match.group(1) != current_unit_token:
                        raise_condomob_layout_error(
                            "totalizador pertence a outra unidade", page_number, displayed_line_number
                        )
                    if int(unit_total_match.group(2)) != current_unit_charge_count:
                        raise_condomob_layout_error(
                            "quantidade de cobranças da unidade não confere",
                            page_number,
                            displayed_line_number,
                        )
                    reported_totals = [
                        parse_br_decimal(unit_total_match.group(index)) for index in range(3, 9)
                    ]
                    if reported_totals != current_unit_totals:
                        raise_condomob_layout_error(
                            "totais da unidade não conferem", page_number, displayed_line_number
                        )
                    current_unit_closed = True
                    table_header_seen = False
                    continue

                row_match = row_end_pattern.search(line)
                if row_match:
                    if current_unit_closed or not table_header_seen or not current_name or not current_document:
                        raise_condomob_layout_error(
                            "cobrança sem unidade, pagador ou tabela ativos",
                            page_number,
                            displayed_line_number,
                        )
                    prefix = line[:row_match.start()].strip()
                    prefix_match = row_prefix_pattern.match(prefix)
                    if not prefix_match:
                        raise_condomob_layout_error(
                            "identificação da cobrança inválida", page_number, displayed_line_number
                        )
                    charge_type = normalize_layout_text(prefix_match.group(1))
                    if charge_type not in allowed_charge_types:
                        raise_condomob_layout_error(
                            "tipo de cobrança desconhecido", page_number, displayed_line_number
                        )
                    printed_payer = prefix_match.group(2).strip()
                    if not any(character.isalpha() for character in printed_payer):
                        raise_condomob_layout_error(
                            "pagador impresso na cobrança é inválido",
                            page_number,
                            displayed_line_number,
                        )

                    reference_month = row_match.group(1) or row_match.group(6)
                    due_date_raw = row_match.group(2) or row_match.group(4)
                    due_date = parse_br_date(due_date_raw)
                    if due_date == due_date_raw:
                        raise_condomob_layout_error(
                            "data de vencimento inválida", page_number, displayed_line_number
                        )
                    amounts = [parse_br_decimal(row_match.group(index)) for index in range(7, 13)]
                    if sum(amounts[:5], Decimal("0.00")) != amounts[5]:
                        raise_condomob_layout_error(
                            "total da cobrança não confere", page_number, displayed_line_number
                        )
                    current_unit_totals = [
                        total + value for total, value in zip(current_unit_totals, amounts)
                    ]
                    grand_totals = [total + value for total, value in zip(grand_totals, amounts)]
                    current_unit_charge_count += 1

                    charges.append(ChargeJSON(
                        extractCondominium=current_condominium,
                        unit=current_unit,
                        originalDraweeName=current_name,
                        originalDraweeDocument=current_document,
                        description=prefix_match.group(1),
                        documentNumber=prefix_match.group(5) or prefix_match.group(4),
                        installment=prefix_match.group(3) or "",
                        referenceMonth=reference_month,
                        dueDate=due_date,
                        originalAmount=float(amounts[0]),
                        penaltyAmount=float(amounts[1]),
                        interestAmount=float(amounts[2]),
                        monetaryCorrection=float(amounts[3]),
                        legalFees=float(amounts[4]),
                        totalAmount=float(amounts[5]),
                    ))
                    continue

                if not current_unit_closed and parties_seen and not table_header_seen:
                    is_contact = (
                        "@" in line
                        or bool(re.search(r"\(\d{2}\)\s*\d", line))
                        or "palmas - to" in normalized_line
                        or line.endswith(";")
                    )
                    if is_contact:
                        continue

                raise_condomob_layout_error(
                    "linha não reconhecida pelo layout homologado", page_number, displayed_line_number
                )

    if not charges or not current_condominium or unit_count == 0:
        raise_condomob_layout_error("nenhuma cobrança válida encontrada")
    if pending_payer or not current_unit_closed:
        raise_condomob_layout_error("última unidade incompleta ou sem totalizador")
    if not summary_header_seen or not summary_complete:
        raise_condomob_layout_error("resumo final ausente ou incompleto")

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
