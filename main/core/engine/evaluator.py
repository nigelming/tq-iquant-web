from typing import List


class Evaluator:
    def evaluate(self, snapshots: List[dict], benchmark_data: list = None) -> dict:
        if not snapshots:
            return {}
        total_return = (snapshots[-1]["total_value"] / snapshots[0]["total_value"]) - 1
        return {
            "total_return": total_return,
            "total_trades": 0,
        }
