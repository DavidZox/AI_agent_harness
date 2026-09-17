class NavBrain:
    def __init__(self):
        """
        初始化導航核心，設定 safe_threshold（安全速度上限）為 1.2 m/s——此
        數值對應 ROBOT_AGENT.md 中「速度評估：物理邊界限制為 1.2 m/s」的安全
        原則，供 calculate_risk() 判斷速度是否超出物理邊界時使用。
        """
        self.safe_threshold = 1.2

    def get_v49_status(self):
        """
        回傳 V4.9 核心引擎的固定狀態描述字串，供 robot_ping_cmd.py 用來快速
        確認核心邏輯與語義拓撲導航節點是否可正常對齊、載入。這是靜態文字
        回報，不讀取任何即時感測器或硬體狀態，成功呼叫只代表程式邏輯本身
        正常，不代表機器人實體硬體狀態正常。

        不接受參數，回傳固定字串。
        """
        return "系統狀態：V4.9 核心引擎運作中。語義拓撲與導航節點對齊正常。"

    def calculate_risk(self, speed):
        """
        評估給定速度是否超出安全邊界（self.safe_threshold，預設 1.2 m/s），
        用於移動前的物理邊界防護檢查。

        參數 speed 可為數字或字串，函式內部會先嘗試 float(speed) 轉型；若
        轉型失敗（ValueError，例如傳入非數字字串），回傳提示錯誤字串而不
        拋出例外，讓呼叫端不需額外包 try/except。

        轉型成功後：speed 超過 safe_threshold 回傳附驚嘆號的警告字串（超出
        物理邊界限制）；否則回傳安全確認字串。回傳值皆為給人看的字串，不
        回傳 boolean。
        """
        try:
            speed = float(speed)
            if speed > self.safe_threshold:
                return f"⚠️ 警告：速度 {speed}m/s 超出物理邊界限制 ({self.safe_threshold}m/s)！"
            return f"✅ 安全：當前速度指令 {speed}m/s 正常。"
        except ValueError:
            return "錯誤：速度參數必須為數字。"