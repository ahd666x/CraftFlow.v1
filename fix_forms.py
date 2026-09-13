class PaintingMaterialRequirementForm(forms.ModelForm):
    class Meta:
        model = PaintingMaterialRequirement
        fields = ['painting_stage', 'process', 'raw_material', 'consumption_per_unit']
        widgets = {
            'painting_stage': forms.Select(attrs={'class': 'form-select'}),
            'process': forms.Select(attrs={'class': 'form-select'}),
            'raw_material': forms.Select(attrs={'class': 'form-select select2-raw-material'}),
            'consumption_per_unit': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': '0.001'}),
        }
        labels = {
            'painting_stage': 'مرحله نقاشی (موقت)',
            'process': 'روند نقاشی',
            'raw_material': 'ماده اولیه',
            'consumption_per_unit': 'مقدار مصرف',
        }