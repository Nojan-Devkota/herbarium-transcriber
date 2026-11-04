"""Darwin Core terms we extract, and the strict schema for Spark's interpretation output.

Interpretation is a separate call from reading: Spark receives the verbatim
transcripts (numbered R1, R2, ...) and maps substrings of them to Darwin Core
terms. Each field must quote the exact `verbatim` substring it came from, which
is what lets us compute a source span and check grounding.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Terms read from labels, in export order, with the guidance given to the model.
LABEL_TERMS: dict[str, str] = {
    "catalogNumber": "Specimen/barcode number of the holding herbarium (often printed near 'Herbarium' or a barcode)",
    "institutionCode": "Acronym of the herbarium, e.g. 'TRH', 'K', 'NY'",
    "scientificName": "Full scientific name as currently determined, including authorship, e.g. 'Botrychium lunaria (L.) Sw.'",
    "family": "Plant family if written, e.g. 'Ophioglossaceae'",
    "recordedBy": "Collector(s), as written after Leg./Coll./Collected by",
    "recordNumber": "Collector's field number (No., n°, #)",
    "eventDate": "Collection date normalized to ISO 8601 at the precision given: YYYY, YYYY-MM or YYYY-MM-DD",
    "country": "Country name in English, e.g. 'Norway' for 'NORVEGIA'",
    "stateProvince": "First-level administrative division (state, province, county in Norway = fylke)",
    "county": "Second-level administrative division if given",
    "municipality": "Municipality / commune if given (e.g. Norwegian kommune)",
    "locality": "Specific locality description, as written (without country/state if those are separate)",
    "habitat": "Habitat or substrate description",
    "verbatimElevation": "Elevation exactly as written, e.g. '900 m', 'ALT. 3000 ft'",
    "verbatimCoordinates": "Coordinates or grid references exactly as written (lat/long, UTM, MGRS)",
    "identifiedBy": "Person who determined the name (Det./Rev./Conf.)",
    "dateIdentified": "Date of determination in ISO 8601 at the precision given",
    "occurrenceRemarks": "Any other notes about the specimen worth keeping",
}

# Terms produced by verification tools, never by the model.
DERIVED_TERMS: dict[str, str] = {
    "acceptedNameUsage": "Accepted name per the GBIF Backbone when the label name is a synonym",
    "taxonID": "GBIF Backbone usage key for scientificName",
    "countryCode": "ISO 3166-1 alpha-2 code for country",
    "decimalLatitude": "Latitude from georeferencing the locality",
    "decimalLongitude": "Longitude from georeferencing the locality",
    "coordinateUncertaintyInMeters": "Uncertainty radius of the georeference",
}

ALL_TERMS: list[str] = [*LABEL_TERMS, *DERIVED_TERMS]

LabelTerm = Literal[
    "catalogNumber",
    "institutionCode",
    "scientificName",
    "family",
    "recordedBy",
    "recordNumber",
    "eventDate",
    "country",
    "stateProvince",
    "county",
    "municipality",
    "locality",
    "habitat",
    "verbatimElevation",
    "verbatimCoordinates",
    "identifiedBy",
    "dateIdentified",
    "occurrenceRemarks",
]


class InterpretedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: LabelTerm
    region: int = Field(description="Number of the transcript the text came from: 1 for R1, 2 for R2, ...")
    verbatim: str = Field(description="The exact substring of that transcript this value is based on, copied character for character")
    value: str = Field(description="The interpreted Darwin Core value")


class InterpretOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[InterpretedField]
