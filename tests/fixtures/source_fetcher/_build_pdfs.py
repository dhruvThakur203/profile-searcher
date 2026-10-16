"""One-off helper to build deterministic PDF fixtures."""

from pathlib import Path


def make_pdf(lines: list[str]) -> bytes:
    y = 720
    ops = [f"BT /F1 12 Tf 72 {y} Td"]
    for i, line in enumerate(lines):
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if i == 0:
            ops.append(f"({safe}) Tj")
        else:
            ops.append(f"0 -18 Td ({safe}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        (b"<< /Length %d >>\nstream\n" % len(stream)) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)


def main() -> None:
    root = Path(__file__).parent
    (root / "row4_ador_inder_raj_vig.pdf").write_bytes(
        make_pdf(
            [
                "ADOR POWERTRON LIMITED - STATEMENT OF UNCLAIMED DIVIDEND 2025",
                "INDER RAJ VIG",
                "55 CAIRNHILL ROAD 27 06 CAIRNHILL PLAZA",
                "SINGAPORE 0922",
                "Shares: 100",
            ]
        )
    )
    (root / "row5_milkfood_mithoo.pdf").write_bytes(
        make_pdf(
            [
                "MILKFOOD LIMITED - UNPAID DIVIDEND 2023-24",
                "MITHOO M RAMCHANDANI",
                "12044, FALCON CREST WAY",
                "NORTHRIDGE, CALIFORNIA 91326",
                "(USA)",
            ]
        )
    )
    (root / "empty_text.pdf").write_bytes(make_pdf([]))


if __name__ == "__main__":
    main()
