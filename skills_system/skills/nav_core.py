class NavBrain:
    def __init__(self):
        self.safe_threshold = 1.2

    def get_v49_status(self):
        return "系統狀態：V4.9 核心引擎運作中。語義拓撲與導航節點對齊正常。"

    def calculate_risk(self, speed):
        try:
            speed = float(speed)
            if speed > self.safe_threshold:
                return f"⚠️ 警告：速度 {speed}m/s 超出物理邊界限制 ({self.safe_threshold}m/s)！"
            return f"✅ 安全：當前速度指令 {speed}m/s 正常。"
        except ValueError:
            return "錯誤：速度參數必須為數字。"