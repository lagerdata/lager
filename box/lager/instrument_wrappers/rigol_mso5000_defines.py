# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from .visa_enum import VisaEnum

class CursorMode(VisaEnum):
    Off = ("OFF")
    Manual = ("MANual")
    Track = ("TRACk")
    XY = ("XY")
    Measure = ("MEASure")

class CursorType(VisaEnum):
    Time = ("TIME")
    Amplitude = ("AMPLitude")

class CursorSource(VisaEnum):
    Channel1 = ("CHANnel1")
    Channel2 = ("CHANnel2")
    Channel3 = ("CHANnel3")
    Channel4 = ("CHANnel4")
    Math1 = ("MATH1")
    Math2 = ("MATH2")
    Math3 = ("MATH3")
    Math4 = ("MATH4")
    Logic = ("LA")
    NoSource = ("NONE")    

class CursorUnit(VisaEnum):
    Second = ("SECond")
    Hertz = ("HZ")
    Degree = ("DEGRee")
    Percent = ("PERCent")

class ScopeChannel(VisaEnum):
    Channel1 = (1)
    Channel2 = (2)
    Channel3 = (3)
    Channel4 = (4)

class TriggerType(VisaEnum):
    Edge = ("EDGE")
    Pulse = ("PULSe")
    Slope = ("SLOPe")
    Video = ("VIDeo")
    Pattern = ("PATTern")
    Duration = ("DURation")
    Timeout = ("TIMeout")
    Runt = ("RUNT")
    Window = ("WINDow")
    Delay = ("DELay")
    Setup = ("SETup")
    NEdge = ("NEDGe")
    UART = ("RS232")
    I2C = ("IIC")
    SPI = ("SPI")
    CAN = ("CAN")
    Flexray = ("FLEXray")
    LIN = ("LIN")
    I2S = ("IIS")
    M1553 = ("M1553")

class TriggerCoupling(VisaEnum):
    AC = ("AC")
    DC = ("DC")
    LF_Reject = ("LFR")
    HF_Reject = ("HFR")

class TriggerStatus(VisaEnum):
    TRIGGERED = ("TD")
    WAITING = ("WAIT")
    TRIGGERING = ("RUN")
    AUTO_TRIGGERING = ("AUTO")
    STOPPED = ("STOP")

class TriggerMode(VisaEnum):
    Auto = ("AUTO")
    Normal = ("NORM")
    Single = ("SING")

class TriggerEdgeSource(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")
    AC_Line = ("ACL")

class TriggerEdgeSlope(VisaEnum):
    Positive = ("POS")
    Negative = ("NEG")
    Either = ("RFAL")

class TriggerSlopeSource(VisaEnum):
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")

class TriggerSlopeCondition(VisaEnum):
    Greater = ("GRE")
    Less = ("LESS")
    GLess = ("GLES")

class TriggerSlopeWindow(VisaEnum):
    TA = ("TA")
    TB = ("TB")
    TAB = ("TAB")

class TriggerPulseSource(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")

class TriggerPulseCondition(VisaEnum):
    Greater = ("GRE")
    Less = ("LESS")
    Box = ("GLES")

class TriggerUARTSource(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")

class TriggerUARTCondition(VisaEnum):
    Start = ("STARt")
    Error = ("ERRor")
    CError = ("CERRor")
    Data = ("DATA")

class TriggerUARTParity(VisaEnum):
    Even = ("EVEN")
    Odd = ("ODD")
    NoParity = ("NONE")
   
   

class TriggerI2CSource(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")

class TriggerI2CCondition(VisaEnum):
    Start = ("STARt")
    ReStart = ("RESTart")
    Stop = ("STOP")
    NACK = ("NACKnowledge")
    Address = ("ADDRess")
    Data = ("DATA")
    AddrData = ("ADATa") 

class TriggerI2CDirection(VisaEnum):
    Read = ("READ")
    Write = ("WRITe")
    RW = ("RWRite")
      

class TriggerSPISource(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")

class TriggerSPICondition(VisaEnum):
    CS = ("CS")
    Timeout = ("TIM")

class TriggerSPISlope(VisaEnum):
    Positive = ("POSitive")
    Negative = ("NEGative") 

class TriggerSPICSMode(VisaEnum):
    High = ("HIGH")
    Low = ("LOW")        
 
class TriggerCANSource(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")

class TriggerCANCondition(VisaEnum):
    SOF = ("SOF")
    EOF = ("EOF")
    IDRemote = ("IDR")
    OverLoad = ("OVER")
    IDFrame = ("IDFR")
    DataFrame = ("DAT")
    IDData = ("IDD")
    ErrorFrame = ("ERFR")
    ErrorAnswer = ("ERAN")
    ErrorCheck = ("ERCH")
    ErrorFormat = ("ERF")
    ErrorRandom = ("ERR")
    ErrorBit = ("ERB")

class TriggerCANSigType(VisaEnum):
    TXRX = ("RXTX")
    CANHigh = ("H")
    CANLow = ("L")
    Differential = ("DIFF")    

class MeasurementSource(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")
    Math1 = ("MATH1")
    Math2 = ("MATH2")
    Math3 = ("MATH3")
    Math4 = ("MATH4")

class MeasurementClear(VisaEnum):
    Item1 = ("ITEM1")
    Item2 = ("ITEM2")
    Item3 = ("ITEM3")
    Item4 = ("ITEM4")
    Item5 = ("ITEM5")
    Item6 = ("ITEM6")
    Item7 = ("ITEM7")
    Item8 = ("ITEM8")
    Item9 = ("ITEM9")
    Item10 = ("ITEM10")
    All = ("ALL")

class MeasurementItem(VisaEnum):
    VMax = ("VMAX")
    VMin = ("VMIN")
    VPP = ("VPP")
    VTop = ("VTOP")
    VBase = ("VBASe")
    VAmp = ("VAMP")
    VAvg = ("VAVG")
    VRMS = ("VRMS")
    Overshoot = ("OVERshoot")
    Preshoot = ("PREShoot")
    MArea = ("MARea")
    MPArea = ("MPARea")
    Period = ("PERiod")
    Frequency = ("FREQuency")
    RTime = ("RTIMe")
    FTime = ("FTIMe")
    PWidth = ("PWIDth")
    NWidth = ("NWIDth")
    PDuty = ("PDUTy")
    NDuty = ("NDUTy")
    TVMax = ("TVMAX")
    TVMin = ("TVMIN")
    PSlewrate = ("PSLewrate")
    NSlewrate = ("NSLewrate")
    VUpper = ("VUPPer")
    VMid = ("VMID")
    VLower = ("VLOWer")
    Variance = ("VARiance")
    PVRMS = ("PVRMs")
    PPulses = ("PPULses")
    NPulses = ("NPULses")
    PEdges = ("PEDGes")
    NEdges = ("NEDGes")
    RRDelay = ("RRDelay")
    RFDelay = ("RFDelay")
    FRDelay = ("FRDelay")
    FFDelay = ("FFDelay")
    RRPhase = ("RRPHase")
    RFPhase = ("RFPHase")
    FRPhase = ("FRPHase")
    FFPhase = ("FFPHase")

class WaveformFormat(VisaEnum):
    CSV = ("CSV")
    RAW = ("RAW")

class MathOperator(VisaEnum):
    Add = ("ADD")
    Subtract = ("SUBTract")
    Multiply = ("MULTiply")
    Divide = ("DIVision")
    And = ("AND")
    Or = ("OR")
    Xor = ("XOR")
    Not = ("NOT")
    FFT = ("FFT")
    Intg = ("INTG")
    Diff = ("DIFF")
    Sqrt = ("SQRT")
    Log = ("LOG")
    Ln = ("LN")
    Exp = ("EXP")
    Abs = ("ABS")
    LowPass = ("LPASs")
    HighPass = ("HPASs")
    BandPass = ("BPASs")
    BandStop = ("BSTop")
    AXB = ("AXB")

class MathSource(VisaEnum):
    Channel1 = ("CHANnel1")
    Channel2 = ("CHANnel2")
    Channel3 = ("CHANnel3")
    Channel4 = ("CHANnel4")
    Ref1 = ("REF1")
    Ref2 = ("REF2")
    Ref3 = ("REF3")
    Ref4 = ("REF4")
    Ref5 = ("REF5")
    Ref6 = ("REF6")
    Ref7 = ("REF7")
    Ref8 = ("REF8")
    Ref9 = ("REF9")
    Ref10 = ("REF10")
    Math1 = ("MATH1")
    Math2 = ("MATH2")
    Math3 = ("MATH3")

MATHSOURCE_TO_CHANNEL = {MathSource.Channel1: 1, MathSource.Channel2: 2, MathSource.Channel3: 3, MathSource.Channel4: 4}

class MathLogicSource(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")

class LogicChannel(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    NoChannel = ("NONE")

class LogicGroup(VisaEnum):
    Group1 = ("GROup1")
    Group2 = ("GROup2")
    Group3 = ("GROup3")
    Group4 = ("GROup4")

class LogicDisplaySize(VisaEnum):
    Small = ("SMALl", "SMAL")
    Medium = ("LARGe", "LARG")
    Large = ("MEDium", "MED")

class LogicPod(VisaEnum):
    Pod1 = ("POD1")
    Pod2 = ("POD2")


class BusMode(VisaEnum):
    Parallel = ("PARallel")
    RS232 = ("RS232")
    SPI = ("SPI")
    I2C = ("IIC")
    I2S = ("IIS")
    LIN = ("LIN")
    CAN = ("CAN")
    FlexRay = ("FLEXray")
    M1553 = ("M1553")

class BusFormat(VisaEnum):
    Hex = ("HEX")
    ASCII = ("ASCii")
    Decimal = ("DEC")
    Binary = ("BIN")

class BusView(VisaEnum):
    Packets = ("PACKets")
    Details = ("DETails")
    Payload = ("PAYLoad")

class BusType(VisaEnum):
    PAL = ("PAL")
    TX = ("TX")
    RX = ("RX")
    SCL = ("SCL")
    SDA = ("SDA")
    CS = ("CS")
    CLK = ("CLK")
    MISO = ("MISO")
    MOSI = ("MOSI")
    LIN = ("LIN")
    CAN = ("CAN")
    CANSub1 = ("CANSub1")
    FLEX = ("FLEX")
    OneFiveFiveThree = ("1553")

class BusLogicSource(VisaEnum):
    D0 = ("D0")
    D1 = ("D1")
    D2 = ("D2")
    D3 = ("D3")
    D4 = ("D4")
    D5 = ("D5")
    D6 = ("D6")
    D7 = ("D7")
    D8 = ("D8")
    D9 = ("D9")
    D10 = ("D10")
    D11 = ("D11")
    D12 = ("D12")
    D13 = ("D13")
    D14 = ("D14")
    D15 = ("D15")
    Channel1 = ("CHAN1")
    Channel2 = ("CHAN2")
    Channel3 = ("CHAN3")
    Channel4 = ("CHAN4")
    Off = ("OFF")

class BusEndianness(VisaEnum):
    MSB = ("MSB")
    LSB = ("LSB")

class BusUARTPolarity(VisaEnum):
    Positive = ("POSitive")
    Negative = ("NEGative")

class BusUARTParity(VisaEnum):
    NoParity = ("NONE")
    Even = ("EVEN")
    Odd = ("ODD")   

class BusUARTPacketEnd(VisaEnum):
    NULL = ("NULL")
    LF = ("LF")
    CR = ("CR")
    SP = ("SP")

class BusI2CAddressMode(VisaEnum):
    Normal = ("NORMal")
    RW = ("RW")

class BusSPISCLSlope(VisaEnum):
    Positive = ("POSitive")
    Negative = ("NEGative")

class BusSPIPolarity(VisaEnum):
    High = ("HIGH")
    Low = ("LOW")

class BusSPIMode(VisaEnum):
    CS = ("CS")
    Timeout = ("TIM")

class BusCANSigType(VisaEnum):
    TX = ("TX")
    RX = ("RX")
    CANHigh = ("CANH")
    CANLow = ("CANL")
    Differential = ("DIFFerential")

class BusFlexRaySigType(VisaEnum):
    BP = ("BP")
    BM = ("BM")
    RT = ("RT")

