"""Offline, fixed-capacity logistic baseline. No Pine or order integration."""
import math
import hashlib
import json
from statistics import mean


def sigmoid(x):
    return 1/(1+math.exp(-max(-35, min(35, x))))


class Logistic:
    def __init__(self, iterations=1000, rate=.05, ridge=.1):
        self.iterations, self.rate, self.ridge = iterations, rate, ridge

    def fit(self, x, y):
        if not x or len(x) != len(y) or len(set(y)) != 2:
            raise ValueError('training requires both classes and complete paired rows')
        width = len(x[0])
        if not width or any(len(row) != width or any(not math.isfinite(v) for v in row) for row in x) or any(v not in (0, 1) for v in y):
            raise ValueError('invalid training values')
        self.means = [mean(row[j] for row in x) for j in range(width)]
        self.scales = [max(1e-9, math.sqrt(mean((row[j]-self.means[j])**2 for row in x))) for j in range(width)]
        data = [[1]+[(v-m)/s for v,m,s in zip(row,self.means,self.scales)] for row in x]
        self.weights = [0.0]*(width+1)
        self.training_sha256 = hashlib.sha256(json.dumps({'x':x,'y':y},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        for _ in range(self.iterations):
            gradient = [0.0]*(width+1)
            for row, label in zip(data,y):
                residual = sigmoid(sum(w*v for w,v in zip(self.weights,row)))-label
                for j in range(width+1):
                    gradient[j] += residual*row[j]/len(data)
            for j in range(width+1):
                self.weights[j] -= self.rate*(gradient[j]+(self.ridge*self.weights[j] if j else 0))
        return self

    def artifact(self):
        return {'algorithm':'fixed_gradient_logistic_v1','iterations':self.iterations,
                'rate':self.rate,'ridge':self.ridge,'means':self.means,
                'scales':self.scales,'weights':self.weights,
                'training_sha256':self.training_sha256,'orders_enabled':False}

    def predict(self, x):
        if any(len(row) != len(self.means) or any(not math.isfinite(v) for v in row) for row in x):
            raise ValueError('invalid prediction row')
        return [sigmoid(self.weights[0]+sum(w*(v-m)/s for w,v,m,s in zip(self.weights[1:],row,self.means,self.scales))) for row in x]


def logit(p):
    p = max(1e-8, min(1-1e-8, p))
    return math.log(p/(1-p))


def score(probabilities, labels):
    if not labels:
        return {'n': 0, 'brier': None, 'log_loss': None, 'calibration_bins': []}
    if len(probabilities) != len(labels) or any(not 0 <= p <= 1 for p in probabilities):
        raise ValueError('invalid prediction probabilities')
    bins = []
    for i in range(5):
        pairs = [(p,y) for p,y in zip(probabilities,labels) if min(int(p*5),4) == i]
        bins.append({'lower': i/5, 'upper': (i+1)/5, 'n': len(pairs), 'mean_probability': mean(p for p,_ in pairs) if pairs else None, 'observed_frequency': mean(y for _,y in pairs) if pairs else None})
    return {'n': len(labels), 'brier': mean((p-y)**2 for p,y in zip(probabilities,labels)), 'log_loss': -mean(y*math.log(max(1e-12,p))+(1-y)*math.log(max(1e-12,1-p)) for p,y in zip(probabilities,labels)), 'calibration_bins': bins}


def evaluate(train, validation, test):
    if len(train) < 10 or len(set(e['y'] for e in train)) != 2:
        return {'status': 'INSUFFICIENT_TRAINING_CLASSES_OR_ROWS', 'train_n': len(train), 'test_n': len(test)}
    model = Logistic().fit([e['x'] for e in train], [e['y'] for e in train])
    predictions = model.predict([e['x'] for e in test])
    result = {'status': 'EXPLORATORY_ONLY', 'train_n': len(train), 'validation_n': len(validation), 'test_n': len(test), 'train_prevalence': mean(e['y'] for e in train), 'train_scaler_means': model.means, 'raw': score(predictions,[e['y'] for e in test])}
    result['model_artifact'] = model.artifact()
    result['test_predictions'] = [{'id':e.get('id'), 'probability':p, 'label':e['y']} for e,p in zip(test,predictions)]
    result['constant_train_prevalence'] = score([result['train_prevalence']]*len(test), [e['y'] for e in test])
    if len(validation) >= 8 and len(set(e['y'] for e in validation)) == 2:
        calibrator = Logistic(ridge=.1).fit([[logit(p)] for p in model.predict([e['x'] for e in validation])], [e['y'] for e in validation])
        result['platt_validation_only'] = score(calibrator.predict([[logit(p)] for p in predictions]), [e['y'] for e in test])
        result['calibration_artifact'] = calibrator.artifact()
    else:
        result['calibration_status'] = 'INSUFFICIENT_VALIDATION_CLASSES_OR_ROWS'
    return result
