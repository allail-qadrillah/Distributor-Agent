"""Tool definitions exposed to the model, pure data, no behavior.

Shape follows the verified Surpluss Intelligence contract: the OpenAI
standard function tool {"type": "function", "function": {...}} passes
through the marketplace unchanged (docs: reference/tool-format-normalization,
checked 2026-11-17). Parameter schemas are hand-written inline JSON Schema
deliberately: no $ref/$defs, so no provider-side resolution risk, and every
constraint stays readable next to the description it guards.

Safety invariants baked into the definitions (P2.2 acceptance):
- No definition carries customer identity: the trusted customer id comes
  from application context (P2.3), never from model arguments.
- No definition carries prices: the backend resolves unit prices from the
  customer's price list; the model must not see a price field to fill.
- confirm_order has no approval argument: explicit user approval lives in
  trusted application context (P2.3/P2.4), never in model-generated
  arguments.

Descriptions are Bahasa Indonesia on purpose: the model serves Indonesian
FMCG customers and the agent instructions are Bahasa too.
"""

from dataclasses import dataclass

SEARCH_PRODUCTS_TOOL_NAME = "search_products"
CREATE_DRAFT_ORDER_TOOL_NAME = "create_draft_order"
CONFIRM_ORDER_TOOL_NAME = "confirm_order"


@dataclass(frozen=True)
class ToolDefinition:
    """One model-visible tool: name, description, JSON Schema parameters.

    Validated at construction so a malformed definition fails at import
    time, not inside a customer conversation. The registry trusts these.
    """

    name: str
    description: str
    parameters: dict

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("tool name must be non-blank")
        if not self.description or not self.description.strip():
            raise ValueError(f"tool {self.name!r} description must be non-blank")
        if not isinstance(self.parameters, dict):
            raise ValueError(f"tool {self.name!r} parameters must be a dict")
        if self.parameters.get("type") != "object":
            raise ValueError(f"tool {self.name!r} parameters must have type object")

    def to_openai_spec(self) -> dict:
        """The exact wire object sent to the provider in `tools`."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


SEARCH_PRODUCTS_TOOL = ToolDefinition(
    name=SEARCH_PRODUCTS_TOOL_NAME,
    description=(
        "Cari produk berdasarkan istilah pencarian (nama produk, alias, atau SKU) "
        "dan kembalikan daftar kandidat beserta harga khusus pelanggan ini dan "
        "stok yang tersedia. Gunakan tool ini SETIAP kali pelanggan menanyakan "
        "produk, harga, atau stok, dan sebelum membuat pesanan. Jika hasilnya "
        "lebih dari satu kandidat, tanyakan klarifikasi ke pelanggan, jangan "
        "memilih salah satu sendiri. Identitas pelanggan TIDAK dikirim sebagai "
        "argumen; sistem menentukannya secara otomatis."
    ),
    parameters={
        "type": "object",
        "properties": {
            "search_term": {
                "type": "string",
                "description": (
                    "Istilah pencarian: nama produk, alias, atau SKU, "
                    "misalnya 'Aqua 600 ml' atau 'AQUA600'."
                ),
            },
        },
        "required": ["search_term"],
        "additionalProperties": False,
    },
)

CREATE_DRAFT_ORDER_TOOL = ToolDefinition(
    name=CREATE_DRAFT_ORDER_TOOL_NAME,
    description=(
        "Buat pesanan DRAFT untuk pelanggan ini. Harga satuan TIDAK dikirim "
        "sebagai argumen dan tidak boleh dikarang: sistem menentukan harga "
        "dari daftar harga pelanggan. product_id HARUS berasal dari hasil "
        "tool search_products, bukan dari teks mentah pelanggan. Draft bukan "
        "pesanan terkonfirmasi: tidak ada reservasi stok dan tidak ada "
        "pemeriksaan kredit pada tahap ini. Setelah draft dibuat, jelaskan "
        "isinya ke pelanggan dan TANYAKAN persetujuan eksplisit sebelum "
        "memanggil confirm_order."
    ),
    parameters={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Daftar item pesanan. Setiap produk hanya boleh muncul "
                    "satu kali; jumlahkan ke satu item bila pelanggan "
                    "menyebut produk yang sama berulang."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "product_id": {
                            "type": "string",
                            "description": ("UUID produk dari hasil search_products."),
                        },
                        "quantity": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Jumlah yang dipesan (unit terkecil).",
                        },
                    },
                    "required": ["product_id", "quantity"],
                    "additionalProperties": False,
                },
            },
            "notes": {
                "type": "string",
                "description": "Catatan opsional untuk pesanan.",
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    },
)

CONFIRM_ORDER_TOOL = ToolDefinition(
    name=CONFIRM_ORDER_TOOL_NAME,
    description=(
        "Konfirmasi pesanan DRAFT milik pelanggan ini menjadi pesanan "
        "terkonfirmasi. Sistem akan memvalidasi stok dan limit kredit, lalu "
        "membuat reservasi stok secara atomik. HANYA panggil tool ini SETELAH "
        "pelanggan memberikan persetujuan eksplisit atas draft yang spesifik "
        "(misalnya 'ya, konfirmasi pesanan itu'). Tanpa persetujuan eksplisit, "
        "jangan memanggil tool ini, tanyakan dulu. Persetujuan tidak dikirim "
        "sebagai argumen; sistem mencatatnya secara terpisah. order_id berasal "
        "dari hasil create_draft_order."
    ),
    parameters={
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "UUID pesanan draft yang akan dikonfirmasi.",
            },
        },
        "required": ["order_id"],
        "additionalProperties": False,
    },
)
