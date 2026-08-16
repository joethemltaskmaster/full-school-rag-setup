import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def generate_running_fee_payments(num_students=50, base_amount=500000, output_file="fee_payments.csv"):
    np.random.seed(42)

    first_names = ["Amina", "Chidi", "Fatima", "David", "Zainab", "Emeka", "Kemi", "Tunde", "Blessing", "Yusuf"]
    last_names = ["Bello", "Okonkwo", "Abubakar", "Adeleke", "Ibrahim", "Eze", "Nnamdi", "Balogun", "Omah", "Danjuma"]
    methods = ["Bank Transfer", "Card Payment", "Mobile Money", "USSD"]

    records = []
    payment_counter = 1000

    for i in range(1, num_students + 1):
        student_id = f"STD-2026-{i:03d}"
        student_name = f"{np.random.choice(first_names)} {np.random.choice(last_names)}"

        # Slight variation around 500,000 for realistic fee differences across courses
        total_due = base_amount + np.random.choice([0, 25000, -25000, 50000])

        # Determine payment pattern (e.g., 2 to 5 installments over 4 months)
        num_installments = np.random.randint(2, 6)
        start_date = datetime(2025, 9, 1)

        cumulative_paid = 0

        for inst in range(num_installments):
            payment_counter += 1
            payment_id = f"PAY-{payment_counter}"

            # Timestamp spacing within a 120-day semester window
            days_offset = (inst + 1) * (120 // num_installments) + np.random.randint(-5, 5)
            pay_date = (start_date + timedelta(days=days_offset)).strftime("%Y-%m-%d")

            # Amount paid logic
            remaining_before = total_due - cumulative_paid
            if inst == num_installments - 1:
                # Final installment (some fully pay off, some leave a partial balance)
                is_full_paid = np.random.choice([True, False], p=[0.75, 0.25])
                amount_paid = remaining_before if is_full_paid else round(
                    remaining_before * np.random.uniform(0.3, 0.7), -3)
            else:
                amount_paid = round((total_due / num_installments) + np.random.randint(-15000, 15000), -3)
                amount_paid = min(amount_paid, remaining_before)

            cumulative_paid += amount_paid
            balance_remaining = max(0, total_due - cumulative_paid)

            status = "Completed" if balance_remaining == 0 else (
                "Overdue" if inst == num_installments - 1 else "Partial")

            records.append({
                "payment_id": payment_id,
                "student_id": student_id,
                "student_name": student_name,
                "academic_term": "2025/2026 First Term",
                "total_amount_due": total_due,
                "payment_date": pay_date,
                "amount_paid": amount_paid,
                "cumulative_paid": cumulative_paid,
                "balance_remaining": balance_remaining,
                "payment_method": np.random.choice(methods),
                "payment_status": status
            })

            if balance_remaining == 0:
                break

    df = pd.DataFrame(records)
    df.to_csv(output_file, index=False)
    print(f"Successfully generated {len(df)} payment logs across {num_students} students in '{output_file}'.")


if __name__ == "__main__":
    generate_running_fee_payments()