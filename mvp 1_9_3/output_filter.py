import re

class IntensityAwareSofter:
    def __init__(self):
        # 강도별 완화 어미 정의
        self.levels = {
            'HIGH': { # 확률 85% 이상: 강한 경고이나 유보적 태도 유지
                'end': "임이 상당히 유력해 보입니다",
                'connect': "이며, 동시에",
                'adverb': "상당 부분"
            },
            'MID': { # 확률 50%~84%: 합리적 의심 및 가능성 제시
                'end': "일 가능성이 높은 것으로 판단됩니다",
                'connect': "일 가능성이 있으며",
                'adverb': "다소"
            },
            'LOW': { # 확률 30%~49%: 미세한 징후 보고 및 주의 환기
                'end': "일 수도 있으니 추가 확인이 필요해 보입니다",
                'connect': "일 수도 있으며",
                'adverb': "일부"
            }
        }

    def get_level(self, prob):
        if prob >= 0.85: return 'HIGH'
        if prob >= 0.50: return 'MID'
        return 'LOW'

    def soften(self, text, prob):
        lvl_key = self.get_level(prob)
        lvl = self.levels[lvl_key]
        
        # 1. 강도 부사 치환 (100%, 무조건 등)
        modified_text = re.sub(r"100%|무조건|확실히", lvl['adverb'], text)
        
        # 2. 연결 어미 치환 (~있고, ~하며)
        modified_text = re.sub(r"있고|하며|이며", lvl['connect'], modified_text)
        
        # 3. 종결 어미 치환 (~입니다, ~확실합니다)
        # 문장 끝의 '다.'를 타겟팅하여 레벨별 어미로 교체
        modified_text = re.sub(r"(입니다|확실합니다|명백합니다)[.\s]?$", lvl['end'] + ".", modified_text)
        
        return modified_text

# 헬퍼 함수
def soften_output_with_intensity(text, prob):
    softer = IntensityAwareSofter()
    return softer.soften(text, prob)