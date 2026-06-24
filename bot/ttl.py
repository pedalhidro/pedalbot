"""Construtores de fragmentos Turtle para os endpoints do amora.

O backend do amora **não computa nada**: o cliente (aqui, o bot) monta o RDF e o amora só
valida (SHACL) e armazena. Estes builders reproduzem exatamente o que o `upload_images.html`
e o `upload_tour.html` emitem, com as IRIs derivadas (`_geo`/`_hash`/`_route`/`_energy`).

Contratos verificados em código:
- Tour: obrigatórios `dcterms:title` + `dcterms:date` (Violation); resto é Warning.
- Image: obrigatórios `dcterms:date` + `schema:locationCreated` (geo).
- Video: obrigatórios `dcterms:date`, `schema:locationCreated`, `schema:duration`,
  `ph:availableResolution` (≥1), `ph:audio`.

Tudo é string-templating com escaping correto; os testes fazem round-trip com rdflib.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Optional

PREFIXES = """\
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix exif:    <http://www.w3.org/2003/12/exif/ns#> .
@prefix nfo:     <http://www.semanticdesktop.org/ontologies/2007/03/22/nfo#> .
@prefix pav:     <http://purl.org/pav/> .
@prefix ph:      <https://pedalhidrografi.co/terms#> .
@prefix phd:     <https://pedalhidrografi.co/data/> .
@prefix prov:    <http://www.w3.org/ns/prov#> .
@prefix qudt:    <http://qudt.org/schema/qudt/> .
@prefix schema:  <https://schema.org/> .
@prefix unit:    <http://qudt.org/vocab/unit/> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .
"""

LICENSE_CC_BY_SA = "https://creativecommons.org/licenses/by-sa/4.0/"
INTENSITIES = ("De boa", "Ok", "Endorfinado", "Frito", "Insano")


def intensity_for_energy(kj: float) -> str:
    """Classificação DERIVADA da energia (kJ) — não é escolha livre do usuário.

    Faixas do SHACL do amora (EnergyEstimateShape, sh:sparql): <150 'De boa', <300 'Ok',
    <500 'Endorfinado', <1000 'Frito', ≥1000 'Insano'. Passar um valor fora da faixa é
    sh:Violation, então o builder sempre deriva daqui.
    """
    if kj < 150:
        return "De boa"
    if kj < 300:
        return "Ok"
    if kj < 500:
        return "Endorfinado"
    if kj < 1000:
        return "Frito"
    return "Insano"


# ── escaping / slugs ─────────────────────────────────────────────────────────
def esc(s: str) -> str:
    """Escapa um literal Turtle de aspas simples (`"..."`)."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def esc_long(s: str) -> str:
    """Escapa um literal de aspas triplas (`\"\"\"...\"\"\"`) — preserva quebras de linha."""
    return s.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def slug_person(name: str) -> str:
    """Nome → localname `pessoa<PascalCase>` (ex.: "João Silva" → "pessoaJoaoSilva")."""
    base = _strip_accents(name)
    parts = [p for p in "".join(c if c.isalnum() else " " for c in base).split() if p]
    pascal = "".join(p[:1].upper() + p[1:] for p in parts) or "Anon"
    return "pessoa" + pascal


def slug_org(name: str) -> str:
    base = _strip_accents(name).lower()
    s = "-".join(p for p in "".join(c if c.isalnum() else " " for c in base).split() if p)
    return "org_" + (s or "org")


def fmt_decimal(x: float) -> str:
    """Decimal Turtle sem notação científica, sem zeros à toa."""
    s = f"{float(x):.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


# ── pessoas / séries novas (declarações inline) ──────────────────────────────
def _people_block(new_people: "dict[str, str] | None") -> str:
    """new_people: {slug_localname: display_name} — só os que ainda não existem no catálogo."""
    if not new_people:
        return ""
    out = []
    for slug, name in new_people.items():
        out.append(f'phd:{slug} a schema:Person ;\n    schema:alternateName "{esc(name)}" .')
    return "\n".join(out) + "\n"


# ── TOUR ─────────────────────────────────────────────────────────────────────
@dataclass
class TourInput:
    tour_id: str
    title: str
    date_iso: str                              # ISO 8601 com TZ (-03:00)
    series_code: Optional[str] = None          # ex.: "PH"
    series_seq: Optional[int] = None
    series_is_new: bool = False
    route_url: Optional[str] = None            # URL RideWithGPS → ph:linkRoute
    instagram_url: Optional[str] = None        # ph:linkInstagram (xsd:anyURI, max 1)
    description: Optional[str] = None
    count_attendee: Optional[int] = None
    count_newcomer: Optional[int] = None
    energy_kj: Optional[float] = None
    intensity: Optional[str] = None
    departed_iso: Optional[str] = None
    arrived_iso: Optional[str] = None
    moving_duration: Optional[str] = None      # xsd:duration, ex.: "PT2H35M"
    measured_kj: Optional[float] = None
    organizer_slug: Optional[str] = None       # localname já resolvido (pessoa* ou org_*)
    author_slugs: list[str] = field(default_factory=list)
    provider_slugs: list[str] = field(default_factory=list)
    attendee_slugs: list[str] = field(default_factory=list)
    newcomer_slugs: list[str] = field(default_factory=list)
    new_people: dict[str, str] = field(default_factory=dict)
    new_orgs: dict[str, str] = field(default_factory=dict)


def build_tour_ttl(t: TourInput) -> str:
    tid = t.tour_id
    props: list[str] = [
        "a ph:Tour",
        f'dcterms:title "{esc(t.title)}"^^xsd:string',
        f'dcterms:date "{esc(t.date_iso)}"^^xsd:dateTime',
    ]
    aux: list[str] = []  # nós derivados / declarações

    if t.series_code and t.series_seq is not None:
        assoc = f"assoc_{t.series_code}_{t.series_seq}"
        props.append(f"ph:inSeriesEdition phd:{assoc}")
        aux.append(
            f"phd:{assoc} a ph:Association ;\n"
            f"    ph:inEventSeries phd:{t.series_code} ;\n"
            f"    ph:sequenceInSeries {int(t.series_seq)} ."
        )
        if t.series_is_new:
            aux.append(f'phd:{t.series_code} a schema:EventSeries ;\n    dcterms:title "{esc(t.series_code)}" .')

    if t.organizer_slug:
        props.append(f"schema:organizer phd:{t.organizer_slug}")

    if t.route_url:
        props.append(f"ph:linkRoute phd:tour_{tid}_route")
        aux.append(
            f"phd:tour_{tid}_route a ph:RouteReference ;\n"
            f"    schema:url <{t.route_url}> ;\n"
            f"    schema:provider ph:rwgps ."
        )

    if t.instagram_url:
        props.append(f'ph:linkInstagram "{esc(t.instagram_url)}"^^xsd:anyURI')

    if t.description:
        props.append(f'dcterms:description """{esc_long(t.description)}"""@pt')

    if t.count_attendee is not None:
        props.append(f"ph:countAttendee {int(t.count_attendee)}")
    if t.count_newcomer is not None:
        props.append(f"ph:countNewcomer {int(t.count_newcomer)}")

    if t.energy_kj is not None:
        props.append(f"ph:energyEstimate phd:tour_{tid}_energy")
        # A intensidade é DERIVADA da energia (regra SHACL do amora), nunca escolha livre.
        intensity = intensity_for_energy(t.energy_kj)
        aux.append(
            f"phd:tour_{tid}_energy a qudt:QuantityValue ;\n"
            f'    qudt:numericValue "{fmt_decimal(t.energy_kj)}"^^xsd:decimal ;\n'
            f"    qudt:hasUnit unit:KiloJ ;\n"
            f'    ph:intensityClassification "{esc(intensity)}" .'
        )

    if t.departed_iso:
        props.append(f'ph:departedAt "{esc(t.departed_iso)}"^^xsd:dateTime')
    if t.arrived_iso:
        props.append(f'ph:arrivedAt "{esc(t.arrived_iso)}"^^xsd:dateTime')
    if t.moving_duration:
        props.append(f'ph:movingDuration "{esc(t.moving_duration)}"^^xsd:duration')

    if t.measured_kj is not None:
        props.append(f"ph:measuredEnergy phd:tour_{tid}_measured")
        aux.append(
            f"phd:tour_{tid}_measured a qudt:QuantityValue ;\n"
            f'    qudt:numericValue "{fmt_decimal(t.measured_kj)}"^^xsd:decimal ;\n'
            f"    qudt:hasUnit unit:KiloJ ."
        )

    for slug in t.author_slugs:
        props.append(f"prov:wasAttributedTo phd:{slug}")
    for slug in t.provider_slugs:
        props.append(f"pav:providedBy phd:{slug}")
    for slug in t.attendee_slugs:
        props.append(f"schema:attendee phd:{slug}")
    for slug in t.newcomer_slugs:
        props.append(f"ph:hasNewcomer phd:{slug}")

    tour_block = f"phd:tour_{tid}\n    " + " ;\n    ".join(props) + " ."

    orgs = ""
    if t.new_orgs:
        orgs = "\n".join(
            f'phd:{slug} a schema:Organization ;\n    schema:name "{esc(name)}" .'
            for slug, name in t.new_orgs.items()
        ) + "\n"

    chunks = [PREFIXES, "", tour_block]
    if aux:
        chunks += ["", "\n\n".join(aux)]
    people = _people_block(t.new_people)
    if people:
        chunks += ["", people.rstrip()]
    if orgs:
        chunks += ["", orgs.rstrip()]
    return "\n".join(chunks) + "\n"


# ── IMAGE ────────────────────────────────────────────────────────────────────
def build_image_ttl(
    *,
    phash: str,
    date_iso: str,
    lat: float,
    lon: float,
    bearing: Optional[float] = None,
    focal: Optional[float] = None,
    tour_id: Optional[str] = None,
    author_slug: Optional[str] = None,
    provider_slug: Optional[str] = None,
    anonymized: bool = False,
    compressed: bool = False,
    license_url: str = LICENSE_CC_BY_SA,
    new_people: "dict[str, str] | None" = None,
) -> str:
    iri = f"image_{phash}"
    props = [
        "a ph:Image",
        f"nfo:hasHash phd:{iri}_hash",
        f'dcterms:date "{esc(date_iso)}"^^xsd:dateTime',
        f"dcterms:license <{license_url}>",
        f"schema:locationCreated phd:{iri}_geo",
    ]
    if bearing is not None:
        props.append(f'exif:gpsImgDirection "{fmt_decimal(bearing)}"^^xsd:decimal')
    if focal is not None:
        props.append(f'exif:focalLengthIn35mmFilm "{fmt_decimal(focal)}"^^xsd:decimal')
    if tour_id:
        props.append(f"ph:capturedDuring phd:tour_{tour_id}")
    if author_slug:
        props.append(f"prov:wasAttributedTo phd:{author_slug}")
    if provider_slug:
        props.append(f"pav:providedBy phd:{provider_slug}")
    if anonymized:
        props.append("ph:anonymized true")
    if compressed:
        props.append("ph:compressed true")

    blocks = [
        f"phd:{iri}\n    " + " ;\n    ".join(props) + " .",
        f'phd:{iri}_hash a nfo:FileHash ;\n    nfo:hashAlgorithm "pHash" ;\n    nfo:hashValue "{phash}" .',
        f"phd:{iri}_geo a schema:GeoCoordinates ;\n"
        f'    schema:latitude "{fmt_decimal(lat)}"^^xsd:decimal ;\n'
        f'    schema:longitude "{fmt_decimal(lon)}"^^xsd:decimal .',
    ]
    chunks = [PREFIXES, "", "\n\n".join(blocks)]
    people = _people_block(new_people)
    if people:
        chunks += ["", people.rstrip()]
    return "\n".join(chunks) + "\n"


# ── VIDEO ────────────────────────────────────────────────────────────────────
def build_video_ttl(
    *,
    vhash: str,
    date_iso: str,
    lat: float,
    lon: float,
    duration_s: float,
    resolutions: list[str],       # ex.: ["audio", "360p", "720p"]
    audio_path: str,              # ex.: "<vhash>.audio.webm"
    thumb_path: Optional[str] = None,
    video360p: Optional[str] = None,
    video720p: Optional[str] = None,
    tour_id: Optional[str] = None,
    author_slug: Optional[str] = None,
    provider_slug: Optional[str] = None,
    license_url: str = LICENSE_CC_BY_SA,
    new_people: "dict[str, str] | None" = None,
) -> str:
    iri = f"video_{vhash}"
    res_literals = ", ".join(f'"{esc(r)}"' for r in resolutions)
    props = [
        "a ph:Video",
        f'dcterms:date "{esc(date_iso)}"^^xsd:dateTime',
        f"dcterms:license <{license_url}>",
        f"schema:locationCreated phd:{iri}_geo",
        f'schema:duration "PT{duration_s:.2f}S"^^xsd:duration',
        f'ph:audio "{esc(audio_path)}"',
        f"ph:availableResolution {res_literals}",
    ]
    if thumb_path:
        props.append(f'schema:thumbnail "{esc(thumb_path)}"')
    if video360p:
        props.append(f'ph:video360p "{esc(video360p)}"')
    if video720p:
        props.append(f'ph:video720p "{esc(video720p)}"')
    if tour_id:
        props.append(f"ph:capturedDuring phd:tour_{tour_id}")
    if author_slug:
        props.append(f"prov:wasAttributedTo phd:{author_slug}")
    if provider_slug:
        props.append(f"pav:providedBy phd:{provider_slug}")

    blocks = [
        f"phd:{iri}\n    " + " ;\n    ".join(props) + " .",
        f"phd:{iri}_geo a schema:GeoCoordinates ;\n"
        f'    schema:latitude "{fmt_decimal(lat)}"^^xsd:decimal ;\n'
        f'    schema:longitude "{fmt_decimal(lon)}"^^xsd:decimal .',
    ]
    chunks = [PREFIXES, "", "\n\n".join(blocks)]
    people = _people_block(new_people)
    if people:
        chunks += ["", people.rstrip()]
    return "\n".join(chunks) + "\n"
