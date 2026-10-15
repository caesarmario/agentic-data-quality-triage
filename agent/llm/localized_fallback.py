"""Indonesian system fallback; no provider calls or translation of stored artifacts."""
from __future__ import annotations


def indonesian_answer(question, alert, platform_alerts, report_context, evidence_rows, history_rows) -> str:
    from agent.llm.copilot import compact_alert, compact_report_context, MAX_CONTEXT_ALERTS, MAX_CONTEXT_EVIDENCE, MAX_CONTEXT_INCIDENT_HISTORY
    rows = [compact_alert(row) for row in list(platform_alerts or [])[:MAX_CONTEXT_ALERTS]]
    if alert is None:
        if not rows:
            return "Tidak ada incident aktif dalam konteks yang dimuat. Ini bukan bukti bahwa seluruh warehouse sehat. Periksa hasil DQ terbaru jika Anda mengharapkan masalah."
        items = "\n".join(f"• `{row['alert_ref']}` | {row['severity']} | `{row['table_name']}` | {row['issue_title']}" for row in rows)
        return f"Ada {len(rows)} incident aktif dalam konteks yang dimuat:\n{items}\nMulai dari severity tertinggi dan buka Investigate sebelum mengusulkan perbaikan."
    item = compact_alert(alert)
    ref = item["alert_ref"]
    q = question.casefold()
    if any(word in q for word in ("history", "riwayat", "sebelumnya")):
        count = min(len(history_rows or []), MAX_CONTEXT_INCIDENT_HISTORY)
        return f"Ada {count} catatan investigasi sebelumnya untuk `{ref}` dalam konteks ini. Riwayat hanya pembanding, bukan bukti penyebab saat ini. Periksa evidence investigasi terbaru."
    if any(word in q for word in ("evidence", "bukti")):
        count = min(len(evidence_rows or []), MAX_CONTEXT_EVIDENCE)
        return f"Konteks jawaban ini memuat {count} evidence untuk `{ref}`. Rincian yang tidak dimuat belum dapat saya pastikan; ini tidak berarti evidence tersebut tidak ada di platform. Buka Evidence atau jalankan investigasi baru tanpa mengubah data."
    report = compact_report_context(report_context)
    if report:
        return (f"Untuk `{ref}`, ringkasan report yang tersimpan: {report.get('summary') or item['issue_title']} "
                f"Rekomendasi yang tercatat: {report.get('recommended_action') or 'Periksa evidence sebelum mengusulkan perbaikan.'} "
                "Teks report dipertahankan dalam bahasa aslinya. Belum ada perbaikan yang dijalankan oleh jawaban ini; remediation memerlukan approval terpisah.")
    return (f"Pemeriksaan `{item['table_name']}` untuk `{ref}` menemukan `{item['observed_value']}`; "
            f"yang diharapkan `{item['expected_value']}`. Buka Investigate untuk memeriksa penyebab dan evidence sebelum mengambil tindakan. Jawaban ini tidak memperbaiki data.")
