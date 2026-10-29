from pathlib import Path
import pandas as pd

def generate(root='samples'):
    root=Path(root); root.mkdir(exist_ok=True)
    historical=pd.DataFrame({'Driver Name':['Maria Lopez','James Wilson','Robert Chen','Aisha Patel','Aisha Patel','Thomas Reed',None], 'Truck':['TR-101','TR-102','TR-103','TR-104','TR-104','bad','TR-107'], 'Safety Officer':['Lee','Lee','Smith','Jones','Jones','Smith','Lee'], 'License':['IL12345','IL23456','IL34567','IL45678','IL45678','INVALID',None], 'Start Date':['2024-01-02','01/03/2024','2024-03-01','2024-01-01','2024-01-01','invalid',None]})
    current=pd.DataFrame({'Full Name':['Maria Lopez','Jim Wilson','Robert Chen','Aisha Patel','Tomas Reed','Elena Garcia'], 'Vehicle Number':['TR-201','TR-102','TR-103','TR-104','TR-106','TR-108'], 'Safety Officer':['Jones','Lee','Smith','Jones','Smith','Lee'], 'License Number':['IL12345','IL23456','IL34567','IL45678','INVALID','IL67890'], 'Effective Date':['2025-01-01']*6})
    for name,frame in [('historical_2024',historical),('current_2025',current)]:
        with pd.ExcelWriter(root/(name+'.xlsx')) as w:
            frame.to_excel(w,sheet_name='Drivers',index=False)
            pd.DataFrame({'Vehicle':['TR-101','TR-102'],'Capacity':[10,20]}).to_excel(w,sheet_name='Vehicles',index=False)
        frame.to_csv(root/(name+'.csv'),index=False)
if __name__=='__main__': generate()
